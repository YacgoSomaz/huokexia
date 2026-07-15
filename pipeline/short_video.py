"""短视频中心的数据层。

解析抖音主页链接、用本机浏览器渲染读取最近作品卡片、保存待拆解任务。
解析阶段只读取用户主动输入的主页，不调用签名接口、不拉取音视频流。
"""

from __future__ import annotations

import json
import atexit
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import imageio_ffmpeg
import requests

from . import ai_report, browser_cookies, config
from .fingerprint import USER_AGENT

_URL_RE = re.compile(r"https?://[^\s]+", re.I)
_PROFILE_URL_RE = re.compile(r"https?://(?:www\.)?douyin\.com/user/[A-Za-z0-9_\-.?=&%]+", re.I)
_DOUYIN_USER_RE = re.compile(r"https?://(?:www\.)?douyin\.com/user/([^/?#\s]+)", re.I)
_VIDEO_ID_RE = re.compile(r"/(?:video|note)/(\d+)")
_MIN_RECENT_COUNT = 1
_MAX_RECENT_COUNT = 100
_SEO_SOURCE_RE = re.compile(r"[?&]source=Baiduspider", re.I)
_PROFILE_CACHE_TTL_SEC = 6 * 3600
_BROWSER_LOCK = threading.Lock()
_RENDER_LOCK = threading.Lock()
_PLAYWRIGHT: Any | None = None
_BROWSER: Any | None = None
_FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
_PREDICTION_BUCKETS = ["低潜力", "普通潜力", "中高潜力", "高潜力", "爆款潜力"]
_SHORT_VIDEO_TEMPLATES: dict[str, list[str]] = {
    "房产短视频": ["开头钩子", "房源/项目卖点", "画面证据", "本地需求命中", "信任背书", "行动引导", "可收藏/可咨询价值", "合规风险提示"],
    "带货短视频": ["痛点命中", "产品价值表达", "使用场景", "信任证明", "优惠/价格表达", "异议处理", "行动引导", "合规风险提示"],
    "泛娱乐/内容短视频": ["情绪吸引力", "开头钩子", "人设辨识度", "叙事完整度", "金句/记忆点", "话题讨论性", "可传播性", "互动引导"],
}
_DEFAULT_TEMPLATE = "泛娱乐/内容短视频"


class ShortVideoError(ValueError):
    """短视频中心输入错误。"""


def _video_key(video: dict[str, Any] | None) -> str:
    """作品去重键：优先作品 id，其次规范化后的作品链接。"""
    if not isinstance(video, dict):
        return ""
    raw_id = str(video.get("id") or video.get("aweme_id") or "").strip()
    if raw_id:
        return raw_id
    url = str(video.get("url") or "").strip()
    if not url:
        return ""
    match = _VIDEO_ID_RE.search(url)
    if match:
        return match.group(1)
    parsed = urlparse(normalize_url(url))
    if parsed.netloc and parsed.path:
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return normalize_url(url)


def _merge_unique_videos(*groups: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """按作品去重合并，保持传入顺序；用于缓存不缩水和翻页合并。"""
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            key = _video_key(item)
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged


def _has_douyin_login_cookie(jar: dict[str, str] | None) -> bool:
    """判断当前 Cookie 是否像完整登录态；弱信任 Cookie 通常只能读主页首屏作品。"""
    return browser_cookies.is_authenticated(jar)


def _load_short_video_cookie_cache() -> tuple[dict[str, str], float]:
    path = config.SHORT_VIDEO_COOKIE_CACHE
    if not path.exists():
        return {}, 0.0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("jar") or {}), float(data.get("ts") or 0)
    except (OSError, ValueError, TypeError):
        return {}, 0.0


def _save_short_video_cookie_cache(jar: dict[str, str]) -> None:
    try:
        config.SHORT_VIDEO_COOKIE_CACHE.write_text(
            json.dumps({"jar": jar, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _short_video_cookie_jar() -> dict[str, str]:
    """All Douyin product modules reuse the one shared authorization jar."""
    shared = browser_cookies.cached_jar()
    legacy, _ = _load_short_video_cookie_cache()
    if browser_cookies.is_authenticated(legacy) and not browser_cookies.is_authenticated(shared):
        browser_cookies.store_shared_jar(legacy)
        return legacy
    return shared or legacy


def short_video_cookie_status() -> dict[str, Any]:
    _short_video_cookie_jar()  # one-time migration from the legacy short-video cache
    return browser_cookies.shared_status()


def remint_short_video_cookie(start_url: str | None = None, *, timeout_sec: int = 180) -> dict[str, Any]:
    """用户手动触发短视频登录授权。

    主页深度下滑常常需要完整登录态；这里只在用户点击时打开浏览器，
    后台解析绝不自动弹窗。
    """
    from playwright.sync_api import sync_playwright

    target = normalize_url(start_url or "https://www.douyin.com/")
    if "douyin.com" not in target:
        target = "https://www.douyin.com/"
    existing = _short_video_cookie_jar()
    existing_verified = bool(browser_cookies.shared_status().get("has_login"))
    with sync_playwright() as p:
        browser = None
        for launch_kwargs in ({"channel": "msedge"}, {}):
            try:
                browser = p.chromium.launch(headless=False, timeout=20000, **launch_kwargs)
                break
            except Exception:  # noqa: BLE001
                continue
        if browser is None:
            return {"ok": False, "message": "无法打开浏览器，请检查 Edge 或 Chromium 环境。"}
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1360, "height": 900},
                locale="zh-CN",
            )
            if existing and existing_verified:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                    for k, v in existing.items()
                ])
            page = ctx.new_page()
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            deadline = time.time() + max(30, min(300, int(timeout_sec)))
            jar: dict[str, str] = {}
            has_login = False
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                jar = {
                    c["name"]: c["value"]
                    for c in ctx.cookies()
                    if c.get("domain", "").endswith("douyin.com")
                }
                has_login = _has_douyin_login_cookie(jar)
                if has_login:
                    break
            if not jar.get("ttwid"):
                return {"ok": False, "message": "没有取得抖音 Cookie，请在弹出的窗口完成登录后重试。"}
            _save_short_video_cookie_cache(jar)
            browser_cookies.store_shared_jar(jar)
            has_login = browser_cookies.mark_login_verified(jar) if has_login else False
            return {
                "ok": True,
                "has_login": has_login,
                "cookie_count": len(jar),
                "message": "短视频授权已保存，可以继续加载更多作品。" if has_login else "已保存基础 Cookie；如仍只能读取 20 条，请在弹窗内完成抖音登录。",
            }
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def _limited_profile_warning(found: int, requested: int, jar: dict[str, str] | None = None) -> str:
    if found >= requested:
        return ""
    if requested > 20 and found >= 20 and not _has_douyin_login_cookie(jar):
        return (
            f"本次只读取到 {found} 条作品。当前是未登录/弱 Cookie 会话，"
            "抖音主页通常只开放前 20 条作品；需要读取更多作品，请先完成短视频账号登录授权。"
        )
    return f"本次只读取到 {found} 条作品，可能是账号作品不足、部分作品未公开或平台接口未继续返回。"


_TRACK_RULES: list[tuple[str, list[str]]] = [
    ("房产置业", ["房产", "买房", "户型", "现房", "精装", "楼盘", "小区", "学区", "首付", "阳台"]),
    ("教育升学", ["招生", "小升初", "高考", "中考", "学校", "学区", "附中", "教育", "录取"]),
    ("汽车出行", ["汽车", "试驾", "新能源", "续航", "油耗", "车主", "提车"]),
    ("本地生活", ["探店", "昆明", "美食", "门店", "活动", "团购", "优惠"]),
    ("知识科普", ["知识", "教程", "避坑", "指南", "攻略", "解析", "干货"]),
    ("娱乐内容", ["搞笑", "剧情", "唱歌", "舞蹈", "挑战", "日常", "vlog"]),
]

_STOP_WORDS = {
    "一个", "这个", "那个", "我们", "你们", "他们", "欢迎", "来到", "来看", "直播",
    "视频", "作品", "今天", "可以", "就是", "什么", "怎么", "进行", "你的", "我的",
}


def first_url(text: str) -> str:
    """从分享文案中提取第一个 URL。"""
    text = (text or "").strip()
    if not text:
        raise ShortVideoError("请输入抖音主页链接")
    compact = re.sub(r"\s+", "", text)
    profile_match = _PROFILE_URL_RE.search(compact)
    if profile_match:
        return profile_match.group(0).rstrip("，。；;、)")
    match = _URL_RE.search(text)
    return match.group(0).rstrip("，。；;、)") if match else text


def normalize_url(raw: str) -> str:
    """清理 URL，去掉 hash，保留 query 以便后续排查来源。"""
    url = first_url(raw)
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ShortVideoError("请输入有效的网址链接")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))


def parse_profile_url(text: str) -> dict[str, str]:
    """解析抖音主页链接，返回 sec_user_id 与规范化链接。"""
    source_url = normalize_url(text)
    match = _DOUYIN_USER_RE.search(source_url)
    if not match:
        raise ShortVideoError("请粘贴抖音用户主页链接，例如 https://www.douyin.com/user/...")
    sec_user_id = match.group(1).strip()
    if not sec_user_id:
        raise ShortVideoError("未能识别抖音用户 ID")
    # 主页分享链接有时会带 vid/source 等参数，浏览器会优先落到单个作品上下文，
    # 导致只能读到部分作品。账号解析阶段强制回到主页作品 tab。
    source_url = f"https://www.douyin.com/user/{sec_user_id}?from_tab_name=main"
    return {
        "source_url": source_url,
        "sec_user_id": sec_user_id,
        "platform": "douyin",
    }


def _validate_recent_count(recent_count: int) -> int:
    try:
        value = int(recent_count)
    except (TypeError, ValueError) as exc:
        raise ShortVideoError("作品读取数量必须是数字") from exc
    if value < _MIN_RECENT_COUNT or value > _MAX_RECENT_COUNT:
        raise ShortVideoError(f"作品读取数量需要在 {_MIN_RECENT_COUNT}-{_MAX_RECENT_COUNT} 条之间")
    return value


def resolve_profile(input_text: str, recent_count: int = 5, *, fetch_videos: bool = True) -> dict[str, Any]:
    """解析账号资料与最近作品。

    ``fetch_videos`` 为测试和降级保留；真实页面是前端渲染，普通 HTTP 只有混淆空壳。
    """
    recent_count = _validate_recent_count(recent_count)
    profile = parse_profile_url(input_text)
    videos: list[dict[str, Any]] = []
    warning = ""
    cached = _read_profile_cache().get(profile["sec_user_id"], {})
    cached_videos = list(cached.get("videos") or []) if cached else []
    if fetch_videos and len(cached_videos) >= recent_count:
        profile.update({k: v for k, v in cached.get("profile", {}).items() if v})
        videos = cached_videos[:recent_count]
        profile["video_count"] = str(len(videos))
        return {"profile": profile, "videos": videos, "warning": "已使用本地作品缓存。"}
    if fetch_videos:
        try:
            rendered = _render_profile(profile["source_url"], recent_count)
            profile.update({k: v for k, v in rendered.get("profile", {}).items() if v})
            rendered_videos = rendered.get("videos", [])
            videos = _merge_unique_videos(rendered_videos, cached_videos)[:recent_count]
            if videos:
                _write_profile_cache(profile["sec_user_id"], {"profile": profile, "videos": videos})
            elif cached:
                profile.update({k: v for k, v in cached.get("profile", {}).items() if v})
                videos = (cached.get("videos") or [])[:recent_count]
                warning = "本次页面作品流未加载完成，已使用本地缓存。"
        except Exception as exc:  # noqa: BLE001  浏览器解析失败不抹掉已识别账号
            if cached:
                profile.update({k: v for k, v in cached.get("profile", {}).items() if v})
                videos = (cached.get("videos") or [])[:recent_count]
                warning = "作品列表读取失败，已使用本地缓存。"
            else:
                warning = f"作品列表读取失败：{type(exc).__name__}"
    elif cached:
        profile.update({k: v for k, v in cached.get("profile", {}).items() if v})
        videos = (cached.get("videos") or [])[:recent_count]
    profile["video_count"] = str(len(videos))
    if videos and not warning and len(videos) < recent_count:
        warning = _limited_profile_warning(len(videos), recent_count, _short_video_cookie_jar())
    return {"profile": profile, "videos": videos, "warning": warning}


def iter_resolve_profile_events(input_text: str, recent_count: int = 5) -> Any:
    """流式解析账号资料和作品；抓到一条作品就 yield 一条事件。"""
    recent_count = _validate_recent_count(recent_count)
    yield {"type": "status", "message": "正在校验主页链接", "progress": 6, "phase": "link"}
    profile = parse_profile_url(input_text)
    yield {"type": "status", "message": "正在检查本地作品缓存", "progress": 10, "phase": "cache"}
    cached = _read_profile_cache().get(profile["sec_user_id"], {})
    cached_videos = list(cached.get("videos") or []) if cached else []
    if len(cached_videos) >= recent_count:
        yield {"type": "status", "message": "命中本地缓存，正在载入作品", "progress": 18, "phase": "cache"}
        profile.update({k: v for k, v in cached.get("profile", {}).items() if v})
        videos = cached_videos[:recent_count]
        profile["video_count"] = str(len(videos))
        yield {"type": "profile", "profile": profile}
        for video in videos:
            yield {"type": "video", "video": video}
        yield {"type": "warning", "message": "已使用本地作品缓存。"}
        yield {"type": "done", "count": len(videos)}
        return

    videos: list[dict[str, Any]] = []
    yield {"type": "profile", "profile": {**profile, "nickname": "正在读取账号资料…", "video_count": "0"}}
    yield {"type": "status", "message": "正在打开抖音主页并复用信任 Cookie", "progress": 14, "phase": "browser"}
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            for event in _render_profile_events(profile, recent_count):
                if event.get("type") == "status":
                    yield event
                    continue
                if event.get("type") == "profile":
                    profile.update({k: v for k, v in event.get("profile", {}).items() if v})
                    yield {"type": "profile", "profile": profile}
                elif event.get("type") == "video":
                    video = event.get("video")
                    if isinstance(video, dict):
                        merged = _merge_unique_videos(videos, [video])
                        if len(merged) > len(videos):
                            videos = merged
                            yield {"type": "video", "video": video}
            if videos:
                profile["video_count"] = str(len(videos))
                cached_videos = list(cached.get("videos") or []) if cached else []
                _write_profile_cache(
                    profile["sec_user_id"],
                    {"profile": profile, "videos": _merge_unique_videos(videos, cached_videos)},
                )
                if len(videos) < recent_count:
                    yield {
                        "type": "warning",
                        "message": _limited_profile_warning(
                            len(videos),
                            recent_count,
                            _short_video_cookie_jar(),
                        ),
                    }
                yield {"type": "done", "count": len(videos)}
                return
            if attempt == 0:
                yield {"type": "status", "message": "暂未读到作品，正在刷新主页重新读取", "progress": 36, "phase": "waiting"}
                _reset_browser()
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if videos:
                break
            if attempt == 0:
                yield {"type": "status", "message": "浏览器冷启动较慢，正在重新连接后再试一次", "progress": 22, "phase": "browser"}
                _reset_browser()
                continue
    if last_exc is not None and not cached and not videos:
        profile["video_count"] = "0"
        yield {"type": "profile", "profile": profile}
        yield {
            "type": "warning",
            "message": f"本次浏览器读取作品失败（{type(last_exc).__name__}），可稍后重试或先手动粘贴作品链接。",
        }
        yield {"type": "done", "count": 0}
        return

    if cached:
        profile.update({k: v for k, v in cached.get("profile", {}).items() if v})
        videos = (cached.get("videos") or [])[:recent_count]
        profile["video_count"] = str(len(videos))
        yield {"type": "profile", "profile": profile}
        for video in videos:
            yield {"type": "video", "video": video}
        yield {"type": "warning", "message": "本次页面作品流未加载完成，已使用本地缓存。"}
        yield {"type": "done", "count": len(videos)}
        return

    profile["video_count"] = "0"
    yield {"type": "profile", "profile": profile}
    yield {"type": "warning", "message": "暂未读取到作品，可能是账号作品较少、页面接口未返回或当前 Cookie 被限制。"}
    yield {"type": "done", "count": 0}


def extract_video_urls(text: str) -> list[str]:
    """从多行文本中提取作品链接，去重保序。"""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.finditer(text or ""):
        try:
            url = normalize_url(match.group(0))
        except ShortVideoError:
            continue
        if "douyin.com" not in urlparse(url).netloc:
            continue
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def video_asset_dir(profile: dict[str, Any] | None, video: dict[str, Any] | None) -> Path:
    """短视频素材缓存目录：short_video_assets/<账号>/<作品>/。"""
    profile = profile or {}
    video = video or {}
    account = _safe_asset_name(str(profile.get("sec_user_id") or profile.get("nickname") or "unknown_account"))
    video_id = str(video.get("id") or "")
    if not video_id:
        match = _VIDEO_ID_RE.search(str(video.get("url") or ""))
        video_id = match.group(1) if match else uuid.uuid4().hex[:10]
    return config.SHORT_VIDEO_ASSET_DIR / account / _safe_asset_name(video_id)


def download_video_cover_asset(profile: dict[str, Any] | None, video: dict[str, Any] | None) -> dict[str, Any]:
    """下载并缓存作品封面，命中本地文件时不重复请求。"""
    video = video or {}
    cover_url = str(video.get("cover_url") or "").strip()
    if not cover_url:
        return {"ok": False, "error": "作品缺少封面链接", "path": "", "cached": False}
    out_dir = video_asset_dir(profile, video)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = _image_ext_from_url(cover_url)
    out_path = out_dir / f"cover{ext}"
    if out_path.exists() and out_path.stat().st_size > 0:
        return {"ok": True, "path": str(out_path), "cached": True, "url": cover_url}
    resp = requests.get(cover_url, headers={"User-Agent": USER_AGENT, "Referer": "https://www.douyin.com/"}, timeout=20)
    resp.raise_for_status()
    content_type = str(resp.headers.get("Content-Type") or "").lower()
    if not out_path.suffix or out_path.suffix == ".img":
        out_path = out_dir / f"cover{_image_ext_from_content_type(content_type)}"
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    tmp_path.write_bytes(resp.content)
    tmp_path.replace(out_path)
    return {"ok": True, "path": str(out_path), "cached": False, "url": cover_url}


def download_video_mp3_asset(
    profile: dict[str, Any] | None,
    video: dict[str, Any] | None,
    *,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    """解析作品播放地址并提取 mp3，缓存命中时直接返回。"""
    video = video or {}
    out_dir = video_asset_dir(profile, video)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "audio.mp3"
    if out_path.exists() and out_path.stat().st_size > 1024:
        return {"ok": True, "path": str(out_path), "cached": True, "play_url": ""}
    video_url = str(video.get("url") or "").strip()
    if not video_url:
        return {"ok": False, "error": "作品缺少播放页链接", "path": str(out_path), "cached": False}
    cached_play_url = str(video.get("play_url") or "").strip()
    if cached_play_url:
        return _extract_mp3_from_play_url(cached_play_url, out_path, max_seconds=max_seconds)
    ytdlp_result = _download_video_mp3_with_ytdlp(video_url, out_dir, out_path, max_seconds=max_seconds)
    if ytdlp_result is not None:
        return ytdlp_result
    play_url = resolve_video_play_url(video_url)
    if not play_url:
        return {"ok": False, "error": "未能解析作品播放地址", "path": str(out_path), "cached": False}
    return _extract_mp3_from_play_url(play_url, out_path, max_seconds=max_seconds)


def _extract_mp3_from_play_url(
    play_url: str,
    out_path: Path,
    *,
    max_seconds: int | None = None,
) -> dict[str, Any]:
    tmp_path = out_path.with_name(out_path.name + ".part")
    cmd = [
        _FFMPEG,
        "-y",
        "-headers",
        f"Referer: https://www.douyin.com/\r\nUser-Agent: {USER_AGENT}\r\n",
        "-i",
        play_url,
        "-vn",
    ]
    if max_seconds and max_seconds > 0:
        cmd += ["-t", str(int(max_seconds))]
    cmd += ["-ar", "16000", "-ac", "1", "-acodec", "libmp3lame", "-f", "mp3", str(tmp_path)]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(90, int(max_seconds or 120) + 90),
        creationflags=_NO_WINDOW,
    )
    size = tmp_path.stat().st_size if tmp_path.exists() else 0
    if proc.returncode != 0 or size <= 1024:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        tail = (proc.stderr or "")[-500:].strip()
        return {"ok": False, "error": f"ffmpeg 提取失败：{tail or proc.returncode}", "path": str(out_path), "cached": False, "play_url": play_url}
    tmp_path.replace(out_path)
    return {"ok": True, "path": str(out_path), "cached": False, "play_url": play_url}


def download_video_assets(profile: dict[str, Any] | None, video: dict[str, Any] | None) -> dict[str, Any]:
    """下载单个作品的封面与 mp3。"""
    cover = download_video_cover_asset(profile, video)
    audio = download_video_mp3_asset(profile, video)
    return {
        "id": str((video or {}).get("id") or ""),
        "title": str((video or {}).get("title") or ""),
        "cover": cover,
        "audio": audio,
        "asset_dir": str(video_asset_dir(profile, video)),
        "ok": bool(cover.get("ok")) or bool(audio.get("ok")),
    }


def download_video_assets_batch(
    profile: dict[str, Any] | None,
    videos: list[dict[str, Any]] | None,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """批量下载作品素材；单个失败不影响后续作品。"""
    results: list[dict[str, Any]] = []
    for video in (videos or [])[: max(1, min(25, int(limit)))]:
        if not isinstance(video, dict):
            continue
        try:
            results.append(download_video_assets(profile, video))
        except Exception as exc:  # noqa: BLE001
            results.append({
                "id": str(video.get("id") or ""),
                "title": str(video.get("title") or ""),
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "asset_dir": str(video_asset_dir(profile, video)),
            })
    return results


def resolve_video_play_url(video_url: str) -> str:
    """解析作品页里的真实可播放地址。优先直接 URL，否则用浏览器读取 video/src 与网络请求。"""
    video_url = normalize_url(video_url)
    if re.search(r"\.(mp4|m3u8)(?:\?|$)", video_url, re.I):
        return video_url
    return _render_video_play_url(video_url)


def parse_video_text(text: str) -> tuple[str, int | None, bool]:
    """把抖音作品卡片文本拆成 (标题, 点赞数, 是否置顶)。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    pinned = False
    like_count: int | None = None
    title_parts: list[str] = []
    for line in lines:
        if line == "置顶":
            pinned = True
            continue
        if like_count is None:
            number = _parse_count(line)
            if number is not None:
                like_count = number
                continue
        title_parts.append(line)
    title = " ".join(title_parts).strip()
    return title, like_count, pinned


def _parse_count(text: str) -> int | None:
    text = (text or "").strip().replace(",", "")
    if not text:
        return None
    try:
        if text.endswith("万"):
            return int(float(text[:-1]) * 10000)
        if text.isdigit():
            return int(text)
    except ValueError:
        return None
    return None


def _safe_asset_name(value: str) -> str:
    value = (value or "").strip() or "unknown"
    value = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value, flags=re.UNICODE).strip("._")
    return (value or "unknown")[:96]


def _image_ext_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".img"


def _image_ext_from_content_type(content_type: str) -> str:
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def _is_playable_media_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower()
    return (
        re.search(r"\.(mp4|m3u8)(?:\?|$)", lower) is not None
        or "mime_type=video_mp4" in lower
        or "/aweme/v1/play/" in lower
        or "playwm" in lower
    )


def _extract_play_urls_from_aweme_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    items: list[Any] = []
    detail = payload.get("aweme_detail")
    if detail:
        items.append(detail)
    items.extend(payload.get("aweme_list") or [])
    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        video = item.get("video") or {}
        for key in ("play_addr", "download_addr", "play_addr_h264"):
            address = video.get(key) or {}
            for url in address.get("url_list") or []:
                url = str(url or "").strip()
                if url and url not in urls:
                    urls.append(url)
    return urls


def _download_video_mp3_with_ytdlp(
    video_url: str,
    out_dir: Path,
    out_path: Path,
    *,
    max_seconds: int | None = None,
) -> dict[str, Any] | None:
    try:
        import yt_dlp  # type: ignore
    except Exception:
        return None

    cookie_file = _write_ytdlp_cookie_file(out_dir)
    temp_template = out_dir / "source.%(ext)s"
    for stale in out_dir.glob("source.*"):
        try:
            stale.unlink()
        except OSError:
            pass
    base_opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(temp_template),
        "ffmpeg_location": str(Path(_FFMPEG).parent),
        "http_headers": {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.douyin.com/",
        },
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    }
    if max_seconds and max_seconds > 0:
        base_opts["postprocessor_args"] = ["-t", str(int(max_seconds))]
    attempts: list[dict[str, Any]] = []
    if sys.platform == "win32":
        attempts.append({"cookiesfrombrowser": ("edge", None, None, None)})
        attempts.append({"cookiesfrombrowser": ("chrome", None, None, None)})
    if cookie_file:
        attempts.append({"cookiefile": str(cookie_file)})
    attempts.append({})
    errors: list[str] = []
    success = False
    try:
        for extra_opts in attempts:
            for stale in out_dir.glob("source.*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            opts = dict(base_opts)
            opts.update(extra_opts)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([video_url])
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                label = "browser-cookie" if "cookiesfrombrowser" in extra_opts else ("cookiefile" if "cookiefile" in extra_opts else "no-cookie")
                errors.append(f"{label}: {exc}")
    finally:
        if cookie_file:
            try:
                cookie_file.unlink(missing_ok=True)
            except OSError:
                pass
    if not success:
        return {"ok": False, "error": "yt-dlp 提取失败：" + " | ".join(errors[-3:]), "path": str(out_path), "cached": False}
    mp3_files = sorted(out_dir.glob("source*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not mp3_files:
        return {"ok": False, "error": "yt-dlp 未生成 mp3 文件", "path": str(out_path), "cached": False}
    tmp_path = out_path.with_name(out_path.name + ".part")
    mp3_files[0].replace(tmp_path)
    size = tmp_path.stat().st_size if tmp_path.exists() else 0
    if size <= 1024:
        tmp_path.unlink(missing_ok=True)
        return {"ok": False, "error": "yt-dlp 生成的 mp3 文件过小", "path": str(out_path), "cached": False}
    tmp_path.replace(out_path)
    return {"ok": True, "path": str(out_path), "cached": False, "play_url": "", "method": "yt-dlp"}


def _write_ytdlp_cookie_file(out_dir: Path) -> Path | None:
    jar = _short_video_cookie_jar()
    if not jar:
        return None
    cookie_file = out_dir / "cookies.txt"
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in jar.items():
        if not name:
            continue
        lines.append(f".douyin.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    cookie_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cookie_file


def _render_video_play_url(video_url: str) -> str:
    with _RENDER_LOCK:
        browser = _get_browser()
        ctx = None
        found: list[str] = []
        aweme_api_urls: list[str] = []
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            jar = _short_video_cookie_jar()
            if jar:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                    for k, v in jar.items()
                ])
            page = ctx.new_page()

            def _capture_response(response: Any) -> None:
                url = str(response.url or "")
                if _is_playable_media_url(url) and url not in found:
                    found.append(url)
                if "/aweme/v1/web/aweme/" in url and url not in aweme_api_urls:
                    aweme_api_urls.append(url)

            page.on("response", _capture_response)
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"font", "image"}
                else route.continue_(),
            )
            try:
                page.goto(video_url, wait_until="commit", timeout=15000)
            except Exception:
                if found:
                    return found[0]
            for _ in range(16):
                try:
                    dom_url = page.evaluate(
                        """() => {
                          const v = document.querySelector('video');
                          return v ? (v.currentSrc || v.src || '') : '';
                        }"""
                    )
                except Exception:
                    dom_url = ""
                if _is_playable_media_url(str(dom_url)):
                    return str(dom_url)
                if found:
                    return found[0]
                page.wait_for_timeout(500)
            try:
                entries = page.evaluate(
                    """() => performance.getEntriesByType('resource')
                      .map(e => e.name)
                      .filter(Boolean)
                      .slice(-200)"""
                )
            except Exception:
                entries = []
            for url in entries or []:
                if _is_playable_media_url(str(url)):
                    return str(url)
            for api_url in aweme_api_urls[:5]:
                try:
                    response = page.request.get(api_url, timeout=8000)
                    data = response.json()
                except Exception:
                    continue
                for play_url in _extract_play_urls_from_aweme_payload(data):
                    if play_url:
                        return play_url
            return found[0] if found else ""
        except Exception:
            _reset_browser()
            raise
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass


def _render_profile(url: str, recent_count: int) -> dict[str, Any]:
    """用 Chromium 渲染用户主页，从 DOM 读取资料与最近作品。"""
    profile: dict[str, Any] = {}
    videos: list[dict[str, Any]] = []
    for event in _render_profile_events(parse_profile_url(url), recent_count):
        if event.get("type") == "profile":
            profile.update(event.get("profile") or {})
        elif event.get("type") == "video":
            video = event.get("video")
            if isinstance(video, dict):
                videos = _merge_unique_videos(videos, [video])
    return {"profile": profile, "videos": videos}


def _render_profile_events(profile: dict[str, str], recent_count: int) -> Any:
    """用 Chromium 渲染用户主页，逐条产出 profile/video 事件。"""
    waited = 0.0
    while not _RENDER_LOCK.acquire(blocking=False):
        waited += 0.8
        yield {
            "type": "status",
            "message": "浏览器正在处理上一个解析任务，正在排队准备",
            "progress": min(16, 10 + int(waited // 2)),
            "phase": "browser",
        }
        time.sleep(0.8)
    try:
        yield from _render_profile_events_locked(profile, recent_count)
    finally:
        _RENDER_LOCK.release()


def _render_profile_events_locked(profile: dict[str, str], recent_count: int) -> Any:
    """实际页面渲染。Playwright sync API 跨线程不安全，所以外层串行化。"""
    url = profile["source_url"]
    browser = _get_browser()
    ctx = None
    try:
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1200},
            locale="zh-CN",
        )
        jar = _short_video_cookie_jar()
        if jar:
            ctx.add_cookies([
                {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                for k, v in jar.items()
            ])
        page = ctx.new_page()
        api_videos: list[dict[str, Any]] = []
        api_profile: dict[str, str] = {}
        post_api_state: dict[str, Any] = {"url": "", "has_more": False, "max_cursor": None}
        post_api_urls: list[str] = []
        processed_post_api_urls: set[str] = set()

        def ingest_profile_api(data: Any, url: str) -> None:
            if not isinstance(data, dict):
                return
            post_api_state["url"] = url
            post_api_state["has_more"] = bool(data.get("has_more"))
            post_api_state["max_cursor"] = data.get("max_cursor")
            for video in _videos_from_aweme_list(data.get("aweme_list") or []):
                if not any(_video_key(v) == _video_key(video) for v in api_videos):
                    api_videos.append(video)
            if data.get("aweme_list"):
                author = (data["aweme_list"][0] or {}).get("author") or {}
                nickname = str(author.get("nickname") or "").strip()
                avatar = (((author.get("avatar_thumb") or {}).get("url_list") or [""])[0] or "").strip()
                if nickname:
                    api_profile["nickname"] = nickname
                if avatar:
                    api_profile["avatar_url"] = avatar

        def capture_profile_api(response: Any) -> None:
            response_url = str(response.url)
            if "/aweme/v1/web/aweme/post/" not in response_url:
                return
            if response_url not in post_api_urls:
                post_api_urls.append(response_url)
            # 优先读取浏览器已经收到的响应体。再次 page.request.get 同一 URL
            # 有时会撞到风控/缓存差异，导致只拿到首屏 20 条。
            try:
                ingest_profile_api(response.json(), response_url)
            except Exception:  # noqa: BLE001
                pass

        page.on("response", capture_profile_api)
        # 深度读取作品墙时要尽量模拟真实用户下滑。抖音的瀑布流有时依赖
        # 图片/视频卡片加载和可视区域变化触发继续加载；这里只拦字体，避免
        # 把继续加载链路误伤成“永远只有首屏 20 条”。
        page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type == "font"
            else route.continue_(),
        )
        yield {"type": "status", "message": "浏览器已就绪，正在进入账号主页", "progress": 18, "phase": "browser"}

        def safe_title() -> str:
            try:
                return page.title() or ""
            except Exception:  # noqa: BLE001
                return ""

        def safe_url() -> str:
            try:
                return page.url
            except Exception:  # noqa: BLE001
                return url

        try:
            page.goto(url, wait_until="commit", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        yield {"type": "status", "message": "主页已打开，正在读取账号资料", "progress": 26, "phase": "profile"}
        seen: set[str] = set()
        fetched_cursors: set[str] = set()
        yielded_profile = False
        empty_reads = 0
        # 快速轮询首屏 DOM，但不要因为短时间没有新增就过早返回。
        # 抖音作品接口经常分批返回；目标数越大，越需要给翻页和滚动更长窗口。
        max_ticks = 44 + min(60, max(0, recent_count - 20) * 3)
        for tick in range(max_ticks):
            for api_url in list(post_api_urls):
                if api_url in processed_post_api_urls:
                    continue
                try:
                    response = page.request.get(api_url, timeout=8000)
                    ingest_profile_api(response.json(), api_url)
                    processed_post_api_urls.add(api_url)
                except Exception:  # noqa: BLE001
                    pass
            title = safe_title()
            if not yielded_profile and (title or tick >= 2):
                avatar = _read_avatar(page)
                yield {
                    "type": "profile",
                    "profile": {
                        **profile,
                        "nickname": _nickname_from_title(title),
                        "avatar_url": avatar,
                        "resolved_url": safe_url(),
                        "title": title,
                    },
                }
                yielded_profile = True
                yield {"type": "status", "message": "已识别账号，正在等待作品列表返回", "progress": 34, "phase": "profile"}
            found_this_tick = 0
            if api_profile and yielded_profile:
                yield {
                    "type": "profile",
                    "profile": {
                        **profile,
                        **api_profile,
                        "resolved_url": safe_url(),
                        "title": safe_title(),
                    },
                }
                api_profile.clear()
            for video in list(api_videos):
                url_key = _video_key(video)
                if not url_key or url_key in seen:
                    continue
                seen.add(url_key)
                found_this_tick += 1
                yield {"type": "video", "video": video}
                yield {
                    "type": "status",
                    "message": f"正在读取作品封面和标题：{len(seen)}/{recent_count}",
                    "progress": 42 + min(48, round(len(seen) / max(1, recent_count) * 48)),
                    "phase": "videos",
                }
                if len(seen) >= recent_count:
                    return
            for video in _read_page_videos(page):
                url_key = _video_key(video)
                if not url_key or url_key in seen:
                    continue
                seen.add(url_key)
                found_this_tick += 1
                yield {"type": "video", "video": video}
                yield {
                    "type": "status",
                    "message": f"正在读取作品封面和标题：{len(seen)}/{recent_count}",
                    "progress": 42 + min(48, round(len(seen) / max(1, recent_count) * 48)),
                    "phase": "videos",
                }
                if len(seen) >= recent_count:
                    return
            if (
                len(seen) < recent_count
                and post_api_state.get("has_more")
                and post_api_state.get("url")
                and post_api_state.get("max_cursor") is not None
            ):
                cursor_key = str(post_api_state.get("max_cursor"))
                if cursor_key and cursor_key not in fetched_cursors:
                    fetched_cursors.add(cursor_key)
                    yield {
                        "type": "status",
                        "message": "正在继续下滑读取更多作品",
                        "progress": min(88, 48 + len(seen) * 4),
                        "phase": "waiting",
                    }
                    try:
                        next_data = _fetch_aweme_post_page(page, str(post_api_state["url"]), cursor_key)
                    except Exception:  # noqa: BLE001
                        next_data = {}
                    if isinstance(next_data, dict):
                        post_api_state["has_more"] = bool(next_data.get("has_more"))
                        post_api_state["max_cursor"] = next_data.get("max_cursor")
                        for video in _videos_from_aweme_list(next_data.get("aweme_list") or []):
                            url_key = _video_key(video)
                            if not url_key or url_key in seen:
                                continue
                            seen.add(url_key)
                            found_this_tick += 1
                            yield {"type": "video", "video": video}
                            yield {
                                "type": "status",
                                "message": f"正在读取作品封面和标题：{len(seen)}/{recent_count}",
                                "progress": 42 + min(48, round(len(seen) / max(1, recent_count) * 48)),
                                "phase": "videos",
                            }
                            if len(seen) >= recent_count:
                                return
            if found_this_tick:
                empty_reads = 0
            else:
                empty_reads += 1
            if tick >= 4 and len(seen) < recent_count:
                if tick in {4, 8, 14, 21, 30, 38, 48, 60, 78, 96}:
                    yield {
                        "type": "status",
                        "message": f"作品接口还在返回中，正在向下滚动加载更多作品（{len(seen)}/{recent_count}）",
                        "progress": min(84, 38 + tick),
                        "phase": "waiting",
                    }
                _scroll_profile_page(page, recent_count)
            has_more = bool(post_api_state.get("has_more"))
            if tick >= 34 and len(seen) >= recent_count:
                return
            if tick >= 48 and seen and empty_reads >= 18 and not has_more:
                return
            page.wait_for_timeout(260 if tick < 10 else 520)
        if not yielded_profile:
            yield {
                "type": "profile",
                "profile": {
                    **profile,
                    "nickname": _nickname_from_title(safe_title()),
                    "avatar_url": _read_avatar(page),
                    "resolved_url": safe_url(),
                    "title": safe_title(),
                },
            }
    except Exception:
        _reset_browser()
        raise
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def _get_browser() -> Any:
    """复用后台浏览器，避免每次解析都付出 Chromium 冷启动成本。"""
    global _PLAYWRIGHT, _BROWSER
    with _BROWSER_LOCK:
        if _BROWSER is not None:
            try:
                if _BROWSER.is_connected():
                    return _BROWSER
            except Exception:  # noqa: BLE001
                _reset_browser_locked()
        from playwright.sync_api import sync_playwright

        _PLAYWRIGHT = sync_playwright().start()
        last_error: Exception | None = None
        for launch_kwargs in ({"channel": "msedge"}, {}):
            try:
                _BROWSER = _PLAYWRIGHT.chromium.launch(headless=True, timeout=15000, **launch_kwargs)
                return _BROWSER
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        _reset_browser_locked()
        raise last_error or RuntimeError("无法启动浏览器")


def _reset_browser() -> None:
    with _BROWSER_LOCK:
        _reset_browser_locked()


def _reset_browser_locked() -> None:
    global _PLAYWRIGHT, _BROWSER
    if _BROWSER is not None:
        try:
            _BROWSER.close()
        except Exception:  # noqa: BLE001
            pass
    if _PLAYWRIGHT is not None:
        try:
            _PLAYWRIGHT.stop()
        except Exception:  # noqa: BLE001
            pass
    _BROWSER = None
    _PLAYWRIGHT = None


atexit.register(_reset_browser)


def prewarm_browser() -> None:
    """后台预热浏览器进程，让第一次真实解析少等一段冷启动时间。"""
    try:
        _get_browser()
    except Exception:  # noqa: BLE001
        pass


def _read_page_videos(page: Any) -> list[dict[str, Any]]:
    items = page.eval_on_selector_all(
        'a[href*="/video/"],a[href*="/note/"]',
        """els => els.map((a, idx) => {
          let box = a;
          for (let i = 0; i < 7 && box && box.parentElement; i++) {
            const img = box.querySelector && box.querySelector('img');
            const text = (box.innerText || box.textContent || '').trim();
            const r = box.getBoundingClientRect();
            if (img && text && r.width >= 80 && r.height >= 100) break;
            box = box.parentElement;
          }
          const img = box.querySelector('img') || a.querySelector('img');
          const cover = img ? (
            img.currentSrc ||
            img.src ||
            img.getAttribute('src') ||
            img.getAttribute('data-src') ||
            img.getAttribute('data-lazy-src') ||
            ''
          ) : '';
          const rect = a.getBoundingClientRect();
          const boxRect = box.getBoundingClientRect();
          return {
            url: a.href,
            text: ((a.innerText || a.textContent || '') + '\\n' + (box.innerText || box.textContent || '')).trim(),
            cover_url: cover,
            index: idx + 1,
            width: Math.round(rect.width || boxRect.width || 0),
            height: Math.round(rect.height || boxRect.height || 0)
          };
        })""",
    )
    return _clean_rendered_videos(items)


def _scroll_until_enough(page: Any, count: int) -> None:
    target = max(1, int(count))
    stagnant = 0
    last_found = -1
    max_rounds = 18 if target <= 25 else 42
    for _ in range(max_rounds):
        found = page.eval_on_selector_all(
            'a[href*="/video/"],a[href*="/note/"]',
            """els => els.filter(a => {
              const box = a.closest('li, div') || a;
              const img = box.querySelector('img') || a.querySelector('img');
              const src = img ? (img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
              const r = a.getBoundingClientRect();
              return src && r.width >= 80 && r.height >= 100 && !/source=Baiduspider/i.test(a.href);
            }).length""",
        )
        if int(found or 0) >= target:
            return
        current = int(found or 0)
        stagnant = stagnant + 1 if current <= last_found else 0
        last_found = max(last_found, current)
        if stagnant >= 5 and current > 0:
            return
        _scroll_profile_page(page, target)
        page.wait_for_timeout(900 if target > 20 else 650)


def _scroll_profile_page(page: Any, target: int) -> int:
    """向下滚动主页和内部滚动容器，触发抖音继续加载作品卡片。"""
    try:
        # 每轮只推进一屏左右，留出时间让瀑布流的 IntersectionObserver
        # 请求下一批作品；一次猛拉到底容易只停在首屏约 20 条。
        page.mouse.move(960, 900)
        page.mouse.wheel(0, 560)
        page.wait_for_timeout(180)
    except Exception:  # noqa: BLE001
        pass
    try:
        return int(page.evaluate(
            """(target) => {
              const step = Math.max(520, Math.floor(window.innerHeight * 0.58));
              const links = Array.from(document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]'))
                .filter(a => !/source=Baiduspider/i.test(a.href || ''));
              const last = links[links.length - 1];
              if (last && last.scrollIntoView) {
                last.scrollIntoView({block:'center', inline:'nearest'});
              }
              const scrollAncestors = [];
              if (last) {
                let el = last.parentElement;
                while (el && el !== document.body && el !== document.documentElement) {
                  const style = getComputedStyle(el);
                  const canScroll = el.scrollHeight > el.clientHeight + 50;
                  if (canScroll || /(auto|scroll)/.test(style.overflowY || '')) scrollAncestors.push(el);
                  el = el.parentElement;
                }
              }
              const candidates = [
                ...scrollAncestors,
                document.scrollingElement,
                document.documentElement,
                document.body,
                ...Array.from(document.querySelectorAll('main, section, [class*="scroll"], [class*="Scroll"], [class*="route-scroll"], [class*="list"], [class*="List"], [class*="waterfall"], [class*="Waterfall"], [data-e2e]'))
              ].filter(Boolean);
              const panel = candidates.find(el => el.scrollHeight > el.clientHeight + 80);
              if (panel) {
                const nextTop = Math.min(panel.scrollHeight - panel.clientHeight, panel.scrollTop + step);
                panel.scrollTop = nextTop;
                panel.dispatchEvent(new Event('scroll', {bubbles:true}));
                panel.dispatchEvent(new WheelEvent('wheel', {deltaY: step, bubbles:true, cancelable:true}));
              }
              window.scrollBy(0, step);
              window.dispatchEvent(new Event('scroll'));
              window.dispatchEvent(new WheelEvent('wheel', {deltaY: step, bubbles:true, cancelable:true}));
              const cards = Array.from(document.querySelectorAll('a[href*="/video/"],a[href*="/note/"]'))
                .filter(a => {
                  const href = a.href || '';
                  if (/source=Baiduspider/i.test(href)) return false;
                  const box = a.closest('li, div') || a;
                  const img = box.querySelector('img') || a.querySelector('img');
                  const src = img ? (img.currentSrc || img.src || img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
                  const r = a.getBoundingClientRect();
                  return src && r.width >= 60 && r.height >= 80;
                });
              return cards.length;
            }""",
            int(target),
        ) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _replace_query(url: str, updates: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({k: str(v) for k, v in updates.items()})
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query), ""))


def _fetch_aweme_post_page(page: Any, url: str, cursor: str) -> dict[str, Any]:
    # 一次取完整批次，减少二次请求恰好被限流后只留下 20 多条作品的概率。
    next_url = _replace_query(url, {"max_cursor": cursor, "count": "35"})
    data = page.evaluate(
        """async (u) => {
          const r = await fetch(u, {
            credentials: 'include',
            headers: {
              accept: 'application/json, text/plain, */*',
              'x-requested-with': 'XMLHttpRequest'
            }
          });
          return await r.json();
        }""",
        next_url,
    )
    return data if isinstance(data, dict) else {}


def _read_avatar(page: Any) -> str:
    try:
        avatar = page.eval_on_selector(
            'img[alt*="头像"]',
            "img => img && img.src",
        )
        if avatar:
            return str(avatar)
    except Exception:  # noqa: BLE001
        pass
    try:
        candidates = page.eval_on_selector_all(
            "img",
            """imgs => imgs.map(i => ({
              src: i.src || '',
              alt: i.alt || '',
              w: i.naturalWidth || 0,
              h: i.naturalHeight || 0
            })).filter(x => x.src && x.w >= 80 && x.h >= 80)""",
        )
    except Exception:  # noqa: BLE001
        return ""
    for item in candidates:
        src = str(item.get("src") or "")
        if "avatar" in src or "avt" in src or "头像" in str(item.get("alt") or ""):
            return src
    return ""


def _nickname_from_title(title: str) -> str:
    title = (title or "").strip()
    for suffix in ("的抖音 - 抖音", " - 抖音"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    return title.strip()


def _clean_rendered_videos(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    videos: list[dict[str, Any]] = []
    for raw in items:
        url = str(raw.get("url") or "").strip()
        if not url:
            continue
        m = _VIDEO_ID_RE.search(url)
        clean_url = f"https://www.douyin.com/video/{m.group(1)}" if m else normalize_url(url)
        if clean_url in seen:
            continue
        cover_url = str(raw.get("cover_url") or "").strip()
        if not cover_url:
            continue
        width = int(raw.get("width") or 0)
        height = int(raw.get("height") or 0)
        if width < 80 or height < 100:
            continue
        seen.add(clean_url)
        title, like_count, pinned = parse_video_text(str(raw.get("text") or ""))
        videos.append({
            "id": (m.group(1) if m else uuid.uuid4().hex[:10]),
            "title": title or f"作品 {len(videos) + 1}",
            "url": clean_url,
            "cover_url": cover_url,
            "like_count": like_count,
            "pinned": pinned,
            "source": "profile",
        })
    return videos


def _videos_from_aweme_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        aweme_id = str(raw.get("aweme_id") or "").strip()
        if not aweme_id:
            continue
        video = raw.get("video") or {}
        cover = (
            ((video.get("cover") or {}).get("url_list") or [""])[0]
            or ((video.get("origin_cover") or {}).get("url_list") or [""])[0]
            or ((video.get("dynamic_cover") or {}).get("url_list") or [""])[0]
            or ""
        )
        stats = raw.get("statistics") or {}
        play_urls = _extract_play_urls_from_aweme_payload({"aweme_detail": raw})
        videos.append({
            "id": aweme_id,
            "title": str(raw.get("desc") or f"作品 {len(videos) + 1}").strip() or f"作品 {len(videos) + 1}",
            "url": f"https://www.douyin.com/video/{aweme_id}",
            "cover_url": str(cover or "").strip(),
            "play_url": play_urls[0] if play_urls else "",
            "like_count": stats.get("digg_count"),
            "pinned": bool(raw.get("is_top")),
            "source": "profile_api",
        })
    return videos


def _profile_cache_path() -> Path:
    return config.SHORT_VIDEO_PROFILE_CACHE_JSON


def _read_profile_cache() -> dict[str, Any]:
    path = _profile_cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    clean: dict[str, Any] = {}
    for key, item in data.items():
        if not isinstance(item, dict):
            continue
        if now - float(item.get("updated_ts") or 0) > _PROFILE_CACHE_TTL_SEC:
            continue
        clean[str(key)] = item
    return clean


def _write_profile_cache(sec_user_id: str, payload: dict[str, Any]) -> None:
    sec_user_id = (sec_user_id or "").strip()
    if not sec_user_id:
        return
    data = _read_profile_cache()
    data[sec_user_id] = {
        "profile": payload.get("profile") or {},
        "videos": payload.get("videos") or [],
        "updated_ts": time.time(),
    }
    path = _profile_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def _store_path() -> Path:
    return config.SHORT_VIDEO_JOBS_JSON


def _benchmark_store_path() -> Path:
    return config.SHORT_VIDEO_BENCHMARKS_JSON


def _read_jobs() -> list[dict[str, Any]]:
    path = _store_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_jobs(jobs: list[dict[str, Any]]) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write_json_list(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    """返回最近的短视频拆解任务。"""
    jobs = _read_jobs()
    jobs.sort(key=lambda j: float(j.get("created_ts") or 0), reverse=True)
    return jobs[: max(1, int(limit))]


def create_job(
    *,
    profile_url: str,
    sec_user_id: str,
    recent_count: int = 5,
    videos: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """创建待拆解任务。videos 为空时表示按最近 N 条作品待抓取。"""
    recent_count = _validate_recent_count(recent_count)
    parsed = parse_profile_url(profile_url)
    sec_user_id = (sec_user_id or parsed["sec_user_id"]).strip()
    clean_videos: list[dict[str, str]] = []
    for idx, item in enumerate(videos or [], start=1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        title = str(item.get("title") or f"作品 {idx}").strip()[:80]
        clean_videos.append({
            "title": title or f"作品 {idx}",
            "url": normalize_url(url),
            "source": str(item.get("source") or "manual"),
            "cover_url": str(item.get("cover_url") or ""),
            "like_count": item.get("like_count"),
            "pinned": bool(item.get("pinned")),
        })
    now = time.time()
    job = {
        "id": uuid.uuid4().hex[:12],
        "platform": "douyin",
        "profile_url": parsed["source_url"],
        "sec_user_id": sec_user_id,
        "recent_count": recent_count,
        "selection_mode": "manual" if clean_videos else "recent",
        "videos": clean_videos,
        "video_count": len(clean_videos) if clean_videos else recent_count,
        "status": "待拆解",
        "created_ts": now,
        "updated_ts": now,
        "note": "自动作品抓取待接入；当前任务已保存，可先用于手动作品拆解。",
    }
    jobs = _read_jobs()
    jobs.insert(0, job)
    _write_jobs(jobs[:200])
    return job


def list_benchmarks(limit: int = 200) -> list[dict[str, Any]]:
    """返回短视频对标账号池。"""
    rows = _read_json_list(_benchmark_store_path())
    rows.sort(key=lambda r: float(r.get("created_ts") or 0), reverse=True)
    return rows[: max(1, int(limit))]


def _find_benchmark(rows: list[dict[str, Any]], benchmark_id: str) -> tuple[int, dict[str, Any]]:
    benchmark_id = (benchmark_id or "").strip()
    if not benchmark_id:
        raise ShortVideoError("缺少对标对象 ID")
    for idx, row in enumerate(rows):
        if str(row.get("id") or "") == benchmark_id:
            return idx, row
    raise ShortVideoError("未找到对标对象")


def create_benchmark(
    *,
    source_profile: dict[str, Any] | None = None,
    positioning: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    profile_url: str = "",
    account_name: str = "",
    note: str = "",
) -> dict[str, Any]:
    """把定位结果里的对标方向或用户输入的账号链接加入对标池。"""
    source_profile = source_profile or {}
    positioning = positioning or {}
    candidate = candidate or {}
    parsed: dict[str, str] = {}
    clean_url = ""
    profile_url = profile_url or str(candidate.get("profile_url") or "")
    if profile_url.strip():
        parsed = parse_profile_url(profile_url)
        clean_url = parsed["source_url"]
    name = (
        account_name
        or candidate.get("name")
        or parsed.get("sec_user_id")
        or "待搜索对标账号"
    )
    track = str(positioning.get("track") or candidate.get("track") or "未分类")
    keywords = positioning.get("benchmark_keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    source_sec_uid = str(source_profile.get("sec_user_id") or positioning.get("profile", {}).get("sec_user_id") or "")
    source_name = str(source_profile.get("nickname") or positioning.get("profile", {}).get("nickname") or "")
    record_type = "account" if clean_url else "direction"
    now = time.time()
    row = {
        "id": uuid.uuid4().hex[:12],
        "type": record_type,
        "platform": "douyin",
        "account_name": str(name).strip()[:80] or "待搜索对标账号",
        "profile_url": clean_url,
        "sec_user_id": parsed.get("sec_user_id", ""),
        "avatar_url": "",
        "track": track,
        "keywords": [str(k).strip() for k in keywords[:8] if str(k).strip()],
        "reason": str(candidate.get("reason") or note or "由账号定位分析推荐加入。")[:200],
        "status": "待搜索" if record_type == "direction" else "待监测",
        "source_profile_sec_user_id": source_sec_uid,
        "source_profile_name": source_name,
        "created_ts": now,
        "updated_ts": now,
        "last_checked_ts": 0,
        "monitoring_note": "后续将用于作品更新、选题变化和互动表现的持续监测。",
    }
    rows = _read_json_list(_benchmark_store_path())
    dedupe_key = row["profile_url"] or (row["source_profile_sec_user_id"] + "::" + row["account_name"])
    kept = [
        r for r in rows
        if (str(r.get("profile_url") or "") or (str(r.get("source_profile_sec_user_id") or "") + "::" + str(r.get("account_name") or ""))) != dedupe_key
    ]
    kept.insert(0, row)
    _write_json_list(_benchmark_store_path(), kept[:500])
    return row


def build_benchmark_search_candidates(row: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    """根据对标方向生成综合作品搜索线索，再从命中作品反推作者主页。"""
    track = str(row.get("track") or "").strip()
    raw_keywords = row.get("keywords") if isinstance(row.get("keywords"), list) else []
    seeds = [track] + [str(k).strip() for k in raw_keywords if str(k).strip()]
    if row.get("type") == "account" and row.get("profile_url"):
        return [{
            "kind": "account",
            "title": str(row.get("account_name") or row.get("sec_user_id") or "真实对标账号"),
            "keyword": str(row.get("account_name") or ""),
            "profile_url": str(row.get("profile_url") or ""),
            "reason": "已是可监测的真实账号，可进入下一步作品解析与持续监测。",
            "status": "待监测",
        }]
    cleaned: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        seed = re.sub(r"\s+", " ", seed).strip(" #")
        if not seed or seed == "未分类" or seed in seen:
            continue
        seen.add(seed)
        cleaned.append(seed)
    result: list[dict[str, Any]] = []
    for keyword in cleaned[: max(1, int(limit))]:
        result.append({
            "kind": "search",
            "search_mode": "content",
            "title": f"综合搜索「{keyword}」相关作品",
            "keyword": keyword,
            "search_url": f"https://www.douyin.com/search/{quote(keyword)}?type=general",
            "reason": "先从综合结果找到内容相近的作品，再反向识别作者主页，优先筛选作品风格和互动表现都接近的对标账号。",
            "status": "待筛选",
        })
    return result


def refresh_benchmark_search(benchmark_id: str) -> dict[str, Any]:
    """为某个对标对象刷新搜索线索，并写回对标池。"""
    rows = _read_json_list(_benchmark_store_path())
    idx, row = _find_benchmark(rows, benchmark_id)
    candidates = build_benchmark_search_candidates(row)
    now = time.time()
    row = {
        **row,
        "search_candidates": candidates,
        "status": "待筛选" if row.get("type") == "direction" and candidates else row.get("status", "待监测"),
        "last_checked_ts": now,
        "updated_ts": now,
        "monitoring_note": "已生成搜索线索；筛选出真实账号后即可加入持续监测。",
    }
    rows[idx] = row
    _write_json_list(_benchmark_store_path(), rows[:500])
    return row


def search_benchmark_accounts(benchmark_id: str, keyword: str = "", limit: int = 8) -> dict[str, Any]:
    """根据搜索线索尝试抓取真实候选账号卡片，并写回对标池。"""
    rows = _read_json_list(_benchmark_store_path())
    idx, row = _find_benchmark(rows, benchmark_id)
    keyword = (keyword or "").strip()
    if not keyword:
        candidates = row.get("search_candidates") if isinstance(row.get("search_candidates"), list) else []
        for item in candidates:
            if isinstance(item, dict) and item.get("kind") == "search" and item.get("keyword"):
                keyword = str(item["keyword"]).strip()
                break
    if not keyword:
        words = row.get("keywords") if isinstance(row.get("keywords"), list) else []
        keyword = str((words or [row.get("track") or ""])[0] or "").strip()
    if not keyword:
        raise ShortVideoError("缺少搜索关键词")
    warning = ""
    try:
        account_candidates = _render_search_work_accounts(keyword, limit=max(1, min(20, int(limit))))
        if not account_candidates:
            account_candidates = _render_search_accounts(keyword, limit=max(1, min(20, int(limit))))
            if account_candidates:
                warning = "综合作品搜索暂未读到作者，已退回账号搜索兜底。"
    except Exception as exc:  # noqa: BLE001
        warning = f"综合作品搜索失败：{type(exc).__name__}"
        try:
            account_candidates = _render_search_accounts(keyword, limit=max(1, min(20, int(limit))))
            if account_candidates:
                warning += "；已退回账号搜索兜底。"
        except Exception as fallback_exc:  # noqa: BLE001
            warning += f"；账号搜索兜底也失败：{type(fallback_exc).__name__}"
            account_candidates = []
    now = time.time()
    row = {
        **row,
        "search_keyword": keyword,
        "account_candidates": account_candidates,
        "status": "候选待选" if account_candidates else "待筛选",
        "last_checked_ts": now,
        "updated_ts": now,
        "search_warning": warning,
        "monitoring_note": "已读取候选账号，可选择加入真实对标池。" if account_candidates else "暂未读到候选账号，可手动打开搜索线索筛选。",
    }
    rows[idx] = row
    _write_json_list(_benchmark_store_path(), rows[:500])
    return row


def recommend_benchmark_accounts(
    *,
    source_profile: dict[str, Any] | None = None,
    positioning: dict[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """把“账号定位 -> 搜索关键词 -> 候选对标账号”压成一步。

    产品上不让用户理解“搜索线索”这层中间概念；技术上仍然复用原来的
    direction 对象，方便后续持续监测和手动补充真实账号。
    """
    source_profile = source_profile or {}
    positioning = positioning or {}
    source_sec_uid = str(
        source_profile.get("sec_user_id")
        or positioning.get("profile", {}).get("sec_user_id")
        or ""
    ).strip()
    if not source_sec_uid:
        raise ShortVideoError("缺少源账号信息，请先解析账号")
    track = str(positioning.get("track") or "未分类").strip() or "未分类"
    raw_keywords = positioning.get("benchmark_keywords") if isinstance(positioning.get("benchmark_keywords"), list) else []
    keywords = [str(k).strip() for k in raw_keywords if str(k).strip()][:8]
    if not keywords:
        keywords = _benchmark_keywords(track, [], [])
    rows = _read_json_list(_benchmark_store_path())
    now = time.time()
    row: dict[str, Any] | None = None
    for idx, existing in enumerate(rows):
        if (
            existing.get("type") == "direction"
            and str(existing.get("source_profile_sec_user_id") or "") == source_sec_uid
            and str(existing.get("track") or "") == track
        ):
            row = {
                **existing,
                "account_name": f"{track} 对标账号推荐",
                "keywords": keywords,
                "reason": "系统根据账号定位自动推荐，可直接筛选候选账号加入监测。",
                "status": "正在推荐",
                "updated_ts": now,
                "monitoring_note": "正在自动读取候选账号，用户无需手动生成搜索线索。",
            }
            rows[idx] = row
            rows.insert(0, rows.pop(idx))
            _write_json_list(_benchmark_store_path(), rows[:500])
            break
    if row is None:
        row = create_benchmark(
            source_profile=source_profile,
            positioning={**positioning, "benchmark_keywords": keywords},
            candidate={
                "name": f"{track} 对标账号推荐",
                "reason": "系统根据账号定位自动推荐，可直接筛选候选账号加入监测。",
            },
        )
    row = refresh_benchmark_search(str(row["id"]))
    keyword = keywords[0] if keywords else ""
    return search_benchmark_accounts(str(row["id"]), keyword=keyword, limit=limit)


def _render_search_work_accounts(keyword: str, limit: int = 8) -> list[dict[str, Any]]:
    """打开抖音综合搜索页，从命中作品卡片中提取作者主页作为对标候选。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    with _RENDER_LOCK:
        browser = _get_browser()
        ctx = None
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            jar = _short_video_cookie_jar()
            if jar:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                    for k, v in jar.items()
                ])
            page = ctx.new_page()
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"font", "media"}
                else route.continue_(),
            )
            search_urls = [
                f"https://www.douyin.com/search/{quote(keyword)}?type=general",
                f"https://www.douyin.com/search/{quote(keyword)}",
            ]
            for search_url in search_urls:
                page.goto(search_url, wait_until="domcontentloaded", timeout=22000)
                for tick in range(16):
                    accounts = _read_search_work_accounts(page, keyword, limit)
                    if len(accounts) >= min(3, limit) or (tick >= 5 and accounts):
                        return accounts[:limit]
                    page.mouse.wheel(0, 760)
                    page.wait_for_timeout(450 if tick < 6 else 820)
            return _read_search_work_accounts(page, keyword, limit)[:limit]
        except Exception:
            _reset_browser()
            raise
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass


def _render_search_accounts(keyword: str, limit: int = 8) -> list[dict[str, Any]]:
    """打开抖音用户搜索页，读取首屏候选账号。页面结构变动时返回空列表。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    with _RENDER_LOCK:
        browser = _get_browser()
        ctx = None
        try:
            ctx = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
            jar = _short_video_cookie_jar()
            if jar:
                ctx.add_cookies([
                    {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                    for k, v in jar.items()
                ])
            page = ctx.new_page()
            page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"font", "media"}
                else route.continue_(),
            )
            page.goto(f"https://www.douyin.com/search/{quote(keyword)}?type=user", wait_until="domcontentloaded", timeout=20000)
            for tick in range(14):
                accounts = _read_search_accounts(page, keyword, limit)
                if len(accounts) >= min(3, limit) or (tick >= 4 and accounts):
                    return accounts[:limit]
                page.mouse.wheel(0, 700)
                page.wait_for_timeout(420 if tick < 6 else 760)
            return _read_search_accounts(page, keyword, limit)[:limit]
        except Exception:
            _reset_browser()
            raise
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass


def _read_search_accounts(page: Any, keyword: str, limit: int = 8) -> list[dict[str, Any]]:
    items = page.eval_on_selector_all(
        'a[href*="/user/"]',
        """els => els.map((a, idx) => {
          let box = a;
          for (let i = 0; i < 5 && box && box.parentElement; i++) {
            const p = box.parentElement;
            if ((p.innerText || '').length > (box.innerText || '').length) box = p;
          }
          const img = box ? box.querySelector('img') : null;
          const text = ((box && box.innerText) || a.innerText || a.textContent || '').trim();
          return {
            url: a.href,
            text,
            avatar_url: img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '',
            index: idx + 1
          };
        })""",
    )
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in items:
        url = normalize_url(str(raw.get("url") or ""))
        match = _DOUYIN_USER_RE.search(url)
        if not match:
            continue
        sec_uid = match.group(1)
        if sec_uid in seen:
            continue
        seen.add(sec_uid)
        text = str(raw.get("text") or "")
        nickname = _nickname_from_search_text(text) or f"{keyword} 相关账号"
        result.append({
            "kind": "account",
            "title": nickname[:80],
            "account_name": nickname[:80],
            "keyword": keyword,
            "profile_url": url,
            "sec_user_id": sec_uid,
            "avatar_url": str(raw.get("avatar_url") or ""),
            "reason": "来自抖音账号搜索结果，建议进一步解析作品后再确认是否作为正式对标。",
            "status": "候选账号",
        })
        if len(result) >= limit:
            break
    return result


def _read_search_work_accounts(page: Any, keyword: str, limit: int = 8) -> list[dict[str, Any]]:
    items = page.eval_on_selector_all(
        'a[href*="/video/"],a[href*="/note/"]',
        """els => els.map((a, idx) => {
          let box = a;
          for (let i = 0; i < 8 && box && box.parentElement; i++) {
            const p = box.parentElement;
            const text = (p.innerText || '').trim();
            if (
              p.querySelector('a[href*="/user/"]') ||
              p.querySelector('img') ||
              text.length > ((box.innerText || '').trim().length + 20)
            ) box = p;
          }
          const userA = box ? box.querySelector('a[href*="/user/"]') : null;
          const imgs = box ? Array.from(box.querySelectorAll('img')) : [];
          const cover = imgs.find(img => {
            const src = img.currentSrc || img.src || img.getAttribute('src') || '';
            const rect = img.getBoundingClientRect ? img.getBoundingClientRect() : {width:0,height:0};
            return src && rect.width >= 80 && rect.height >= 80;
          }) || imgs[0] || null;
          const avatar = userA ? userA.querySelector('img') : null;
          const text = ((box && box.innerText) || a.innerText || a.textContent || '').trim();
          return {
            video_url: a.href,
            profile_url: userA ? userA.href : '',
            author_text: userA ? (userA.innerText || userA.textContent || '').trim() : '',
            text,
            cover_url: cover ? (cover.currentSrc || cover.src || cover.getAttribute('src') || '') : '',
            avatar_url: avatar ? (avatar.currentSrc || avatar.src || avatar.getAttribute('src') || '') : '',
            index: idx + 1
          };
        })""",
    )
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in items:
        profile_url = normalize_url(str(raw.get("profile_url") or ""))
        match = _DOUYIN_USER_RE.search(profile_url)
        if not match:
            continue
        sec_uid = match.group(1)
        if sec_uid in seen:
            continue
        seen.add(sec_uid)
        raw_text = str(raw.get("text") or "")
        author_text = str(raw.get("author_text") or "")
        account_name = _nickname_from_search_text(author_text) or _nickname_from_search_text(raw_text) or f"{keyword} 相关作者"
        work_title, like_count, pinned = parse_video_text(raw_text)
        if not work_title:
            work_title = _search_work_title(raw_text, keyword)
        video_url = normalize_url(str(raw.get("video_url") or ""))
        video_id_match = _VIDEO_ID_RE.search(video_url)
        if video_id_match:
            video_url = f"https://www.douyin.com/video/{video_id_match.group(1)}"
        representative_work = {
            "title": work_title[:120] or f"{keyword} 相关作品",
            "url": video_url,
            "cover_url": str(raw.get("cover_url") or ""),
            "like_count": like_count,
            "pinned": pinned,
        }
        result.append({
            "kind": "account",
            "source": "content_search",
            "title": account_name[:80],
            "account_name": account_name[:80],
            "keyword": keyword,
            "profile_url": profile_url,
            "sec_user_id": sec_uid,
            "avatar_url": str(raw.get("avatar_url") or raw.get("cover_url") or ""),
            "representative_work": representative_work,
            "reason": f"综合搜索命中相关作品《{representative_work['title'][:32]}》，建议解析该作者主页后确认是否适合作为对标。",
            "status": "作品命中候选",
        })
        if len(result) >= limit:
            break
    return result


def _search_work_title(text: str, keyword: str = "") -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    blacklist = {"关注", "粉丝", "获赞", "作品", "用户", "直播中", "点赞", "评论", "分享"}
    candidates: list[str] = []
    for line in lines:
        if line in blacklist:
            continue
        if _parse_count(line) is not None:
            continue
        if re.search(r"https?://|douyin\.com", line):
            continue
        if len(line) > 140:
            line = line[:140]
        if len(line) >= 4:
            candidates.append(line)
    for line in candidates:
        if keyword and keyword in line:
            return line
    return candidates[0] if candidates else ""


def _nickname_from_search_text(text: str) -> str:
    for line in [x.strip() for x in (text or "").splitlines() if x.strip()]:
        if line in {"关注", "粉丝", "获赞", "作品", "用户", "直播中"}:
            continue
        if len(line) > 40:
            continue
        if re.search(r"https?://|douyin\.com", line):
            continue
        return line
    return ""


def analyze_positioning(profile: dict[str, Any] | None, videos: list[dict[str, Any]] | None) -> dict[str, Any]:
    """基于账号作品做轻量定位初判，为后续 AI/对标搜索提供结构化输入。"""
    profile = profile or {}
    clean_videos = [v for v in (videos or []) if isinstance(v, dict)]
    titles = [str(v.get("title") or "").strip() for v in clean_videos if str(v.get("title") or "").strip()]
    joined = "\n".join(titles)
    hashtags = _extract_hashtags(joined)
    keywords = _top_keywords(titles, hashtags)
    track, track_scores = _infer_track(joined, hashtags)
    likes = [int(v.get("like_count") or 0) for v in clean_videos]
    avg_like = round(sum(likes) / len(likes), 1) if likes else 0
    top_video = max(clean_videos, key=lambda v: int(v.get("like_count") or 0), default={})
    pinned_count = sum(1 for v in clean_videos if v.get("pinned"))
    search_keywords = _benchmark_keywords(track, keywords, hashtags)
    return {
        "profile": {
            "nickname": profile.get("nickname") or "待识别账号",
            "sec_user_id": profile.get("sec_user_id") or "",
            "avatar_url": profile.get("avatar_url") or "",
        },
        "track": track,
        "confidence": _confidence(track_scores, len(titles)),
        "tags": keywords[:8],
        "hashtags": hashtags[:10],
        "content_summary": _content_summary(track, keywords, clean_videos),
        "audience_positioning": _audience_positioning(track, keywords),
        "benchmark_keywords": search_keywords,
        "benchmark_accounts": _benchmark_placeholders(track, search_keywords),
        "monitoring_plan": _monitoring_plan(track),
        "metrics": {
            "video_count": len(clean_videos),
            "pinned_count": pinned_count,
            "avg_like": avg_like,
            "top_like": int(top_video.get("like_count") or 0) if top_video else 0,
            "top_title": top_video.get("title") or "",
        },
        "next_step": "下一步可用这些关键词搜索同赛道账号，建立对标账号池并持续监测其新作品、话题变化和互动表现。",
    }


def _extract_hashtags(text: str) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for match in re.finditer(r"#([\u4e00-\u9fffA-Za-z0-9_]{2,24})", text or ""):
        tag = match.group(1).strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _top_keywords(titles: list[str], hashtags: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    for tag in hashtags:
        counts[tag] = counts.get(tag, 0) + 4
    for title in titles:
        clean = re.sub(r"https?://\S+|#[\u4e00-\u9fffA-Za-z0-9_]+", " ", title)
        for word in re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z0-9]{2,18}", clean):
            if word in _STOP_WORDS:
                continue
            counts[word] = counts.get(word, 0) + 1
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:16]]


def _infer_track(text: str, hashtags: list[str]) -> tuple[str, dict[str, int]]:
    haystack = (text or "") + "\n" + " ".join(hashtags)
    scores: dict[str, int] = {}
    for track, words in _TRACK_RULES:
        scores[track] = sum(haystack.count(word) for word in words)
    best = max(scores.items(), key=lambda kv: kv[1], default=("未分类", 0))
    return (best[0] if best[1] > 0 else "未分类"), scores


def _confidence(scores: dict[str, int], title_count: int) -> int:
    if not scores or title_count <= 0:
        return 20
    ordered = sorted(scores.values(), reverse=True)
    top = ordered[0] if ordered else 0
    second = ordered[1] if len(ordered) > 1 else 0
    return max(35, min(92, 45 + top * 6 + (top - second) * 4))


def _content_summary(track: str, keywords: list[str], videos: list[dict[str, Any]]) -> str:
    if not videos:
        return "暂未读取到足够作品，无法形成稳定内容定位。"
    core = "、".join(keywords[:5]) if keywords else "账号核心内容"
    return f"账号当前更接近「{track}」赛道，内容主要围绕 {core} 展开，适合按同赛道账号做横向对标。"


def _audience_positioning(track: str, keywords: list[str]) -> str:
    if track == "房产置业":
        return "潜在受众是有明确置业、改善、学区或本地生活需求的人群。"
    if track == "教育升学":
        return "潜在受众是学生家长、升学决策者以及关注教育资源的人群。"
    if track == "汽车出行":
        return "潜在受众是准备购车、换车或关注出行成本的人群。"
    if track == "本地生活":
        return "潜在受众是关注本地消费、优惠活动和生活服务的人群。"
    if track == "娱乐内容":
        return "潜在受众是以放松、陪伴和情绪价值为主要需求的泛娱乐用户。"
    if keywords:
        return f"潜在受众与 {keywords[0]}、{keywords[1] if len(keywords) > 1 else '相关话题'} 相关。"
    return "潜在受众仍需更多作品和互动数据进一步识别。"


def _benchmark_keywords(track: str, keywords: list[str], hashtags: list[str]) -> list[str]:
    base = [track] if track != "未分类" else []
    merged = base + keywords[:5] + hashtags[:5]
    seen: set[str] = set()
    result: list[str] = []
    for item in merged:
        item = str(item or "").strip("# ")
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result[:8]


def _benchmark_placeholders(track: str, keywords: list[str]) -> list[dict[str, Any]]:
    names = keywords[:4] or ([track] if track != "未分类" else ["同赛道账号"])
    return [
        {
            "name": f"{name} 赛道对标账号",
            "reason": "建议搜索并加入监测，用于对比作品频率、选题结构、封面话术和互动表现。",
            "status": "待搜索",
        }
        for name in names
    ]


def _monitoring_plan(track: str) -> list[str]:
    common = ["每日监测新作品数量与发布时间", "记录爆款作品标题、封面关键词和互动数据", "每周复盘对标账号高频选题变化"]
    if track == "房产置业":
        return ["重点监测户型、价格、区位、学区和交付节点类内容"] + common
    if track == "娱乐内容":
        return ["重点监测高互动话题、粉丝评论方向和强关系互动"] + common
    return common


def score_short_video_work(
    work: dict[str, Any],
    *,
    account_history: list[dict[str, Any]] | None = None,
    template: str = "auto",
) -> dict[str, Any]:
    """用内容评分工作流给单条短视频/脚本打潜力分。"""
    clean_work = _clean_work_input(work)
    selected_template = _select_content_template(template, clean_work)
    fallback = _fallback_score_result(clean_work, selected_template)
    result = _short_video_ai_json(
        "score",
        {
            "work": clean_work,
            "account_history": _clean_history(account_history),
            "template": selected_template,
            "available_templates": list(_SHORT_VIDEO_TEMPLATES.keys()),
            "template_dimensions": _SHORT_VIDEO_TEMPLATES[selected_template],
            "output_contract": {
                "overall_score": "0-100整数",
                "prediction_bucket": _PREDICTION_BUCKETS,
                "confidence": "0-1小数",
                "dimensions": "每个维度含 name/score/max_score/evidence/suggestion",
            },
        },
        fallback,
    )
    return _normalize_score_result(result, fallback)


def predict_short_video_performance(
    work: dict[str, Any],
    *,
    account_history: list[dict[str, Any]] | None = None,
    template: str = "auto",
    score_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """预测相对表现区间，不输出具体播放量。"""
    clean_work = _clean_work_input(work)
    selected_template = _select_content_template(template, clean_work)
    fallback = _fallback_prediction_result(clean_work)
    result = _short_video_ai_json(
        "predict",
        {
            "work": clean_work,
            "account_history": _clean_history(account_history),
            "template": selected_template,
            "score_result": score_result or _fallback_score_result(clean_work, selected_template),
            "allowed_prediction_buckets": _PREDICTION_BUCKETS,
            "rule": "只预测相对区间，不预测具体播放量、GMV、ROI或成交归因。",
        },
        fallback,
    )
    return _normalize_prediction_result(result, fallback)


def learn_from_benchmark_account(
    profile: dict[str, Any] | None,
    videos: list[dict[str, Any]] | None,
    *,
    template: str = "auto",
) -> dict[str, Any]:
    """从对标账号作品总结可复用内容套路。"""
    clean_videos = [_clean_work_input(v) for v in (videos or []) if isinstance(v, dict)]
    sample_text = "\n".join(v.get("title", "") + "\n" + v.get("transcript", "") for v in clean_videos[:5])
    selected_template = _select_content_template(template, {"title": sample_text, "transcript": sample_text})
    fallback = {
        "opening_patterns": [],
        "title_patterns": [],
        "cover_patterns": [],
        "selling_points": [],
        "action_guides": [],
        "hit_commonalities": [],
        "low_performance_issues": [],
        "reusable_script_templates": [],
    }
    result = _short_video_ai_json(
        "learn",
        {
            "profile": profile or {},
            "videos": clean_videos[:12],
            "template": selected_template,
            "goal": "提炼对标账号可学习的开头、标题、封面、卖点、行动引导和爆款共同点。",
        },
        fallback,
    )
    return {key: _as_str_list(result.get(key)) for key in fallback}


def retro_short_video_prediction(
    *,
    prediction: dict[str, Any],
    actual_metrics: dict[str, Any],
    work: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """发布后复盘预测准确性，帮助下一次校准。"""
    fallback = {
        "accuracy": "待判断",
        "bias_reason": "实际数据不足，暂时无法判断预测偏差。",
        "overestimated_reasons": [],
        "underestimated_reasons": [],
        "dimension_adjustments": [],
        "next_adjustments": [],
    }
    result = _short_video_ai_json(
        "retro",
        {
            "work": _clean_work_input(work or {}),
            "prediction": prediction or {},
            "actual_metrics": actual_metrics or {},
            "rule": "只复盘预测偏差，不引入GMV、ROI或成交归因。",
        },
        fallback,
    )
    normalized = dict(fallback)
    normalized["accuracy"] = str(result.get("accuracy") or fallback["accuracy"])[:40]
    normalized["bias_reason"] = str(result.get("bias_reason") or fallback["bias_reason"])[:300]
    for key in ["overestimated_reasons", "underestimated_reasons", "dimension_adjustments", "next_adjustments"]:
        normalized[key] = _as_str_list(result.get(key))
    return normalized


def _short_video_ai_json(task: str, payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    cfg = ai_report.load_config()
    if not cfg.ready:
        raise ShortVideoError("请先在系统设置中配置 AI Key 后再使用短视频 AI 分析。")
    system_prompt = (
        "你是短视频内容增长分析师。请根据输入数据进行内容评分、爆款预测、对标学习或发布后复盘。"
        "必须只输出合法 JSON，不要输出 Markdown。不要预测具体播放量，不要提 GMV、ROI、成交归因。"
        "敏感词和合规问题只作为风险提示，不要严重影响总分。"
    )
    user_prompt = json.dumps({"task": task, "payload": payload}, ensure_ascii=False)
    try:
        raw = ai_report._chat_completion(
            cfg,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.15,
            max_tokens=2600,
            response_format={"type": "json_object"},
        )
        parsed = _json_object_from_text(raw)
        return parsed if isinstance(parsed, dict) else fallback
    except (ValueError, TypeError) as exc:
        safe = dict(fallback)
        safe["_fallback"] = True
        safe["_fallback_reason"] = f"AI返回结构异常，已使用本地兜底结果：{exc}"
        return safe
    except ai_report.AIReportError as exc:
        safe = dict(fallback)
        safe["_fallback"] = True
        safe["_fallback_reason"] = f"AI请求异常，已使用本地兜底结果：{exc}"
        return safe


def _clean_work_input(work: dict[str, Any]) -> dict[str, Any]:
    work = work or {}
    metrics = work.get("metrics") if isinstance(work.get("metrics"), dict) else {}
    return {
        "id": str(work.get("id") or work.get("aweme_id") or "")[:80],
        "title": str(work.get("title") or work.get("desc") or "")[:500],
        "url": str(work.get("url") or "")[:800],
        "cover_description": str(work.get("cover_description") or work.get("vision_summary") or "")[:1000],
        "transcript": str(work.get("transcript") or work.get("audio_transcript") or "")[:20000],
        "like_count": _safe_int(work.get("like_count") if work.get("like_count") is not None else metrics.get("like_count")),
        "comment_count": _safe_int(work.get("comment_count") if work.get("comment_count") is not None else metrics.get("comment_count")),
        "collect_count": _safe_int(work.get("collect_count") if work.get("collect_count") is not None else metrics.get("collect_count")),
        "share_count": _safe_int(work.get("share_count") if work.get("share_count") is not None else metrics.get("share_count")),
    }


def _clean_history(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [_clean_work_input(item) for item in (history or []) if isinstance(item, dict)][:25]


def _select_content_template(template: str, work: dict[str, Any]) -> str:
    if template in _SHORT_VIDEO_TEMPLATES:
        return template
    text = f"{work.get('title', '')}\n{work.get('cover_description', '')}\n{work.get('transcript', '')}"
    estate_words = ["房", "户型", "楼盘", "小区", "首付", "学区", "现房", "置业", "样板间", "大横厅"]
    commerce_words = ["下单", "链接", "优惠", "到手", "同款", "拍下", "发货", "买", "价格", "福利"]
    if any(word in text for word in estate_words):
        return "房产短视频"
    if any(word in text for word in commerce_words):
        return "带货短视频"
    return _DEFAULT_TEMPLATE


def _fallback_score_result(work: dict[str, Any], template: str) -> dict[str, Any]:
    dimensions = []
    for name in _SHORT_VIDEO_TEMPLATES.get(template, _SHORT_VIDEO_TEMPLATES[_DEFAULT_TEMPLATE]):
        dimensions.append({
            "name": name,
            "score": 3,
            "max_score": 5,
            "evidence": "等待 AI 根据标题、封面、转写和互动数据进一步判断。",
            "suggestion": "补充作品转写、封面描述和历史表现后再分析。",
        })
    return {
        "overall_score": 60,
        "prediction_bucket": "普通潜力",
        "confidence": 0.35,
        "template": template,
        "dimensions": dimensions,
        "highlights": [],
        "problems": [],
        "rewrite_suggestions": [],
        "compliance_flags": [],
    }


def _fallback_prediction_result(work: dict[str, Any]) -> dict[str, Any]:
    return {
        "prediction_bucket": "普通潜力",
        "confidence": 0.35,
        "reasons": ["等待 AI 结合账号历史作品表现进一步判断。"],
        "similar_samples": [],
        "may_win": [],
        "may_lose": [],
    }


def _normalize_score_result(result: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    out = dict(fallback)
    out["overall_score"] = max(0, min(100, _safe_int(result.get("overall_score"), fallback["overall_score"])))
    out["prediction_bucket"] = _bucket(result.get("prediction_bucket"), fallback["prediction_bucket"])
    out["confidence"] = _safe_float(result.get("confidence"), fallback["confidence"], 0.0, 1.0)
    out["template"] = str(result.get("template") or fallback["template"])
    dims = result.get("dimensions") if isinstance(result.get("dimensions"), list) else fallback["dimensions"]
    out["dimensions"] = [_normalize_dimension(item) for item in dims if isinstance(item, dict)][:10]
    for key in ["highlights", "problems", "rewrite_suggestions", "compliance_flags"]:
        out[key] = _as_str_list(result.get(key))
    if result.get("_fallback"):
        out["_fallback"] = True
        out["_fallback_reason"] = str(result.get("_fallback_reason") or "AI分析已降级为本地兜底结果。")[:300]
    return out


def _normalize_prediction_result(result: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    out = dict(fallback)
    out["prediction_bucket"] = _bucket(result.get("prediction_bucket"), fallback["prediction_bucket"])
    out["confidence"] = _safe_float(result.get("confidence"), fallback["confidence"], 0.0, 1.0)
    for key in ["reasons", "may_win", "may_lose"]:
        out[key] = _as_str_list(result.get(key))
    samples = result.get("similar_samples") if isinstance(result.get("similar_samples"), list) else []
    out["similar_samples"] = [
        {"title": str(item.get("title") or "")[:120], "reason": str(item.get("reason") or "")[:240]}
        for item in samples if isinstance(item, dict)
    ][:5]
    if result.get("_fallback"):
        out["_fallback"] = True
        out["_fallback_reason"] = str(result.get("_fallback_reason") or "AI预测已降级为本地兜底结果。")[:300]
    return out


def _normalize_dimension(item: dict[str, Any]) -> dict[str, Any]:
    max_score = max(1, _safe_int(item.get("max_score"), 5))
    return {
        "name": str(item.get("name") or "维度")[:40],
        "score": max(0, min(max_score, _safe_int(item.get("score"), 0))),
        "max_score": max_score,
        "evidence": str(item.get("evidence") or "")[:400],
        "suggestion": str(item.get("suggestion") or "")[:400],
    }


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:400] for item in value if str(item).strip()][:12]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:400]]
    return []


def _bucket(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text if text in _PREDICTION_BUCKETS else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = default
    return max(low, min(high, num))


def _json_object_from_text(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI 返回不是 JSON 对象")
    return data
