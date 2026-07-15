"""Comment lead radar for public Douyin video comments.

This module is intentionally a small adapter layer: the short-video center can
discover new works, then this module captures public comments for each video and
turns them into follow-up leads. It does not implement signing, captcha bypass,
or automated private messaging.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse, urlunparse

from . import browser_cookies, config, short_video

VIDEO_ID_RE = re.compile(r"(?:/video/|[?&](?:aweme_id|modal_id|vid)=)(\d{8,})")
USER_ID_RE = re.compile(r"/user/([^/?#]+)")
REPLY_EXPAND_LABEL_RE = re.compile(
    r"^(?:(?:展开|查看)(?:(?:全部|共)?\d*条?)?回复|(?:全部|共)\d+条?回复|\d+条?回复)$"
)
SHORT_URL_RE = re.compile(r"https?://[^\s，。；;、)]+")
AUTH_COOKIE_NAMES = {
    "sessionid",
    "sessionid_ss",
    "sid_guard",
    "sid_tt",
    "uid_tt",
    "uid_tt_ss",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "passport_auth_status",
    "passport_auth_status_ss",
}
FIELDNAMES = [
    "aweme_id",
    "comment_id",
    "parent_comment_id",
    "level",
    "content",
    "comment_ip_location",
    "like_count",
    "create_time",
    "reply_count",
    "commenter_sec_uid",
    "commenter_unique_id",
    "commenter_nickname",
    "commenter_signature",
    "commenter_profile_url",
    "source_url",
    "captured_at",
    "status",
    "ai_label",
    "assigned_to",
]


@dataclass
class CaptureResult:
    ok: bool
    rows: list[dict[str, Any]]
    metadata: dict[str, Any]
    error: str = ""


def _now() -> int:
    return int(time.time())


def _default_store() -> dict[str, Any]:
    return {"version": 1, "monitors": [], "leads": [], "jobs": []}


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_store() -> dict[str, Any]:
    store = _load_json(config.COMMENT_LEADS_JSON, _default_store())
    if not isinstance(store, dict):
        return _default_store()
    store.setdefault("version", 1)
    store.setdefault("monitors", [])
    store.setdefault("leads", [])
    store.setdefault("jobs", [])
    changed = False
    for monitor in store.get("monitors", []):
        if not isinstance(monitor, dict):
            continue
        raw_url = str(monitor.get("raw_url") or monitor.get("target_url") or "")
        aweme_id = str(monitor.get("aweme_id") or extract_aweme_id(raw_url) or "")
        author_sec_uid = str(monitor.get("author_sec_uid") or extract_author_sec_uid(raw_url) or "")
        if author_sec_uid:
            profile_url = f"https://www.douyin.com/user/{author_sec_uid}?from_tab_name=main"
            if monitor.get("target_type") != "profile" or monitor.get("target_url") != profile_url:
                old_id = str(monitor.get("id") or "")
                new_id = f"profile_{author_sec_uid}"
                if old_id and old_id != new_id:
                    for lead in store.get("leads", []):
                        if isinstance(lead, dict) and lead.get("monitor_id") == old_id:
                            lead["monitor_id"] = new_id
                    for job in store.get("jobs", []):
                        if isinstance(job, dict) and job.get("monitor_id") == old_id:
                            job["monitor_id"] = new_id
                monitor.setdefault("raw_url", raw_url)
                monitor["id"] = new_id
                monitor["target_type"] = "profile"
                monitor["target_url"] = profile_url
                monitor["author_sec_uid"] = author_sec_uid
                monitor.setdefault("max_videos", 5)
                changed = True
        elif aweme_id and monitor.get("target_url") != _canonical_video_url(raw_url, aweme_id):
            monitor.setdefault("raw_url", raw_url)
            monitor["target_url"] = _canonical_video_url(raw_url, aweme_id)
            monitor["aweme_id"] = aweme_id
            changed = True
        if author_sec_uid and not monitor.get("author_sec_uid"):
            monitor["author_sec_uid"] = author_sec_uid
            changed = True
        cached_meta = _profile_cache_metadata(author_sec_uid)
        for key, value in cached_meta.items():
            if value and not monitor.get(key):
                monitor[key] = value
                changed = True
        if monitor.get("author_name") and monitor.get("title") in {"", "抖音视频评论", "待识别账号"}:
            monitor["title"] = monitor["author_name"]
            changed = True
    if changed:
        save_store(store)
    return store


def save_store(store: dict[str, Any]) -> None:
    _save_json(config.COMMENT_LEADS_JSON, store)


def extract_first_url(text: str) -> str:
    match = SHORT_URL_RE.search(text or "")
    return match.group(0).rstrip("，。；;、)") if match else (text or "").strip()


def extract_aweme_id(url: str) -> str:
    match = VIDEO_ID_RE.search(url or "")
    return match.group(1) if match else ""


def extract_author_sec_uid(url: str) -> str:
    match = USER_ID_RE.search(url or "")
    return match.group(1) if match else ""


def _canonical_video_url(source_url: str, aweme_id: str) -> str:
    if aweme_id:
        return f"https://www.douyin.com/video/{aweme_id}"
    return source_url


def _profile_cache_metadata(sec_uid: str) -> dict[str, str]:
    sec_uid = (sec_uid or "").strip()
    if not sec_uid:
        return {}
    data = _load_json(config.SHORT_VIDEO_PROFILE_CACHE_JSON, {})
    if not isinstance(data, dict):
        return {}
    item = data.get(sec_uid) if isinstance(data.get(sec_uid), dict) else {}
    profile = item.get("profile") if isinstance(item, dict) else {}
    if not isinstance(profile, dict):
        return {}
    name = str(profile.get("nickname") or "").strip()
    avatar = str(profile.get("avatar_url") or "").strip()
    return {
        "author_name": name,
        "author_avatar": avatar,
        "author_profile_url": f"https://www.douyin.com/user/{sec_uid}",
    }


def add_monitor(
    url: str,
    *,
    title: str = "",
    owner: str = "",
    max_comments: int = 100,
    max_videos: int = 5,
) -> dict[str, Any]:
    raw_url = extract_first_url(url)
    if not raw_url:
        raise ValueError("缺少视频链接")
    aweme_id = extract_aweme_id(raw_url)
    author_sec_uid = extract_author_sec_uid(raw_url)
    is_profile_monitor = bool(author_sec_uid)
    if is_profile_monitor:
        source_url = short_video.parse_profile_url(raw_url)["source_url"]
        monitor_id = f"profile_{author_sec_uid}"
    else:
        source_url = _canonical_video_url(raw_url, aweme_id)
        monitor_id = aweme_id or str(abs(hash(source_url)))
    cached_meta = _profile_cache_metadata(author_sec_uid)
    store = load_store()
    monitors = store["monitors"]
    existing = next((m for m in monitors if m.get("id") == monitor_id), None)
    display_title = (
        title.strip()
        or cached_meta.get("author_name")
        or (existing or {}).get("title")
        or ("待识别账号" if author_sec_uid else "抖音视频评论")
    )
    row = {
        "id": monitor_id,
        "platform": "douyin",
        "target_type": "profile" if is_profile_monitor else "video",
        "target_url": source_url,
        "raw_url": raw_url,
        "aweme_id": aweme_id,
        "title": display_title,
        "author_sec_uid": author_sec_uid or (existing or {}).get("author_sec_uid", ""),
        "author_name": cached_meta.get("author_name") or (existing or {}).get("author_name", ""),
        "author_avatar": cached_meta.get("author_avatar") or (existing or {}).get("author_avatar", ""),
        "author_profile_url": cached_meta.get("author_profile_url") or (existing or {}).get("author_profile_url", ""),
        "video_title": (existing or {}).get("video_title", ""),
        "video_cover": (existing or {}).get("video_cover", ""),
        "owner": owner.strip(),
        "max_comments": max(1, min(int(max_comments or 100), 2000)),
        "max_videos": max(1, min(int(max_videos or 5), 50)),
        "enabled": True,
        "created_at": existing.get("created_at") if existing else _now(),
        "updated_at": _now(),
        "last_run_at": existing.get("last_run_at") if existing else 0,
        "last_error": existing.get("last_error", "") if existing else "",
        "last_count": existing.get("last_count", 0) if existing else 0,
    }
    if existing:
        existing.update(row)
    else:
        monitors.insert(0, row)
    save_store(store)
    return row


def list_monitors() -> list[dict[str, Any]]:
    return list(load_store().get("monitors", []))


def list_leads(*, status: str = "", keyword: str = "", limit: int = 500) -> list[dict[str, Any]]:
    rows = list(load_store().get("leads", []))
    if status:
        rows = [r for r in rows if str(r.get("status") or "") == status]
    if keyword:
        kw = keyword.strip().lower()
        rows = [
            r for r in rows
            if kw in str(r.get("content") or "").lower()
            or kw in str(r.get("commenter_nickname") or "").lower()
            or kw in str(r.get("comment_ip_location") or "").lower()
        ]
    return rows[: max(1, min(int(limit or 500), 5000))]


def _comment_profile_url(sec_uid: str) -> str:
    return f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""


def normalize_comment(
    raw: dict[str, Any],
    *,
    aweme_id: str,
    source_url: str,
    parent_comment_id: str = "",
    level: int = 1,
) -> dict[str, Any]:
    user = raw.get("user") or {}
    sec_uid = str(user.get("sec_uid") or user.get("sec_user_id") or "")
    return {
        "aweme_id": str(raw.get("aweme_id") or aweme_id),
        "comment_id": str(raw.get("cid") or raw.get("comment_id") or ""),
        "parent_comment_id": str(parent_comment_id or ""),
        "level": max(1, int(level or 1)),
        "content": str(raw.get("text") or raw.get("content") or ""),
        "comment_ip_location": str(raw.get("ip_label") or raw.get("ip_location") or ""),
        "like_count": raw.get("digg_count") or raw.get("like_count") or 0,
        "create_time": raw.get("create_time") or "",
        "reply_count": raw.get("reply_comment_total") or 0,
        "commenter_sec_uid": sec_uid,
        "commenter_unique_id": str(user.get("unique_id") or ""),
        "commenter_nickname": str(user.get("nickname") or ""),
        "commenter_signature": str(user.get("signature") or ""),
        "commenter_profile_url": _comment_profile_url(sec_uid),
        "source_url": source_url,
        "captured_at": _now(),
        "status": "待联系",
        "ai_label": "",
        "assigned_to": "",
    }


def normalize_comment_tree(
    raw: dict[str, Any],
    *,
    aweme_id: str,
    source_url: str,
    parent_comment_id: str = "",
    level: int = 1,
) -> list[dict[str, Any]]:
    """Normalize one comment and replies included inline by Douyin's list API."""
    row = normalize_comment(
        raw,
        aweme_id=aweme_id,
        source_url=source_url,
        parent_comment_id=parent_comment_id,
        level=level,
    )
    rows = [row]
    parent_id = str(row.get("comment_id") or "")
    for key in ("reply_comment", "reply_comments", "reply_list"):
        replies = raw.get(key)
        if not isinstance(replies, list):
            continue
        for reply in replies:
            if isinstance(reply, dict):
                rows.extend(
                    normalize_comment_tree(
                        reply,
                        aweme_id=aweme_id,
                        source_url=source_url,
                        parent_comment_id=parent_id,
                        level=level + 1,
                    )
                )
    return rows


def reply_statistics(raw_comments: list[Any]) -> dict[str, Any]:
    """Summarize reply counts announced by top-level comment API responses."""
    reported = 0
    embedded = 0
    parent_ids: list[str] = []
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue
        try:
            reply_total = max(0, int(comment.get("reply_comment_total") or 0))
        except (TypeError, ValueError):
            reply_total = 0
        seen_embedded: set[str] = set()
        anonymous_embedded = 0
        for key in ("reply_comment", "reply_comments", "reply_list"):
            replies = comment.get(key)
            if not isinstance(replies, list):
                continue
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                reply_id = str(reply.get("cid") or reply.get("comment_id") or "")
                if reply_id:
                    seen_embedded.add(reply_id)
                else:
                    anonymous_embedded += 1
        embedded_count = len(seen_embedded) + anonymous_embedded
        reported += reply_total
        embedded += embedded_count
        if reply_total > embedded_count:
            parent_id = str(comment.get("cid") or comment.get("comment_id") or "")
            if parent_id:
                parent_ids.append(parent_id)
    return {
        "reported": reported,
        "embedded": embedded,
        "remaining": max(0, reported - embedded),
        "parent_ids": parent_ids,
    }


def reply_page_summary(parent_comment_id: str, payload: dict[str, Any], comments: list[Any]) -> dict[str, Any]:
    """Keep non-sensitive pagination state for a captured reply page."""
    try:
        cursor = int(payload.get("cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    try:
        total = int(payload.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return {
        "parent_comment_id": str(parent_comment_id or ""),
        "rows": len(comments),
        "cursor": cursor,
        "has_more": payload.get("has_more") in (True, 1, "1"),
        "total": total,
    }


def reply_next_page_url(url: str, cursor: int) -> str:
    """Advance the captured browser reply URL without changing its other parameters."""
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    updated = [(key, str(cursor) if key == "cursor" else value) for key, value in query]
    if not any(key == "cursor" for key, _ in updated):
        updated.append(("cursor", str(cursor)))
    return urlunparse(parsed._replace(query=urlencode(updated)))


def _append_unique_rows(target: list[dict[str, Any]], rows: list[dict[str, Any]], seen_ids: set[str], limit: int) -> None:
    for row in rows:
        if len(target) >= limit:
            return
        if not str(row.get("content") or "").strip():
            continue
        comment_id = str(row.get("comment_id") or "")
        key = comment_id or json.dumps(row, ensure_ascii=False, sort_keys=True)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        target.append(row)


def _scroll_comment_panel(page: Any) -> None:
    """Advance the comment list itself before falling back to the page scroll."""
    page.evaluate(
        """() => {
          const isScrollable = el => el && el.scrollHeight > el.clientHeight + 12;
          const panels = new Set();
          const seeds = Array.from(document.querySelectorAll(
            '[class*="comment" i], [id*="comment" i], [data-e2e*="comment" i]'
          ));
          for (const seed of seeds) {
            let node = seed;
            for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
              if (isScrollable(node)) panels.add(node);
            }
          }
          if (!panels.size && isScrollable(document.scrollingElement)) panels.add(document.scrollingElement);
          for (const panel of panels) {
            const step = Math.max(560, Math.floor(panel.clientHeight * 0.86));
            panel.scrollTop = Math.min(panel.scrollHeight - panel.clientHeight, panel.scrollTop + step);
            panel.dispatchEvent(new Event('scroll', {bubbles: true}));
            panel.dispatchEvent(new WheelEvent('wheel', {deltaY: step, bubbles: true, cancelable: true}));
          }
          window.scrollBy(0, 420);
        }"""
    )


def _expand_comment_replies(page: Any) -> int:
    """Open visible reply threads with trusted Playwright pointer events."""
    expanded = 0
    for _ in range(20):
        target = page.evaluate(
            """() => {
              const replyText = /^(?:(?:展开|查看)(?:(?:全部|共)?\\d*条?)?回复|(?:全部|共)\\d+条?回复|\\d+条?回复)$/;
              for (const el of Array.from(document.querySelectorAll('button, [role="button"], span, a'))) {
                const text = String(el.innerText || el.textContent || '').replace(/\\s+/g, '');
                if (!replyText.test(text)) continue;
                const node = el.closest('button, [role="button"], a') || el;
                if (node.dataset.livewatchReplyExpanded === text) continue;
                node.scrollIntoView({block: 'center', inline: 'center'});
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                node.dataset.livewatchReplyExpanded = text;
                return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
              }
              return null;
            }"""
        )
        if not isinstance(target, dict):
            break
        try:
            x, y = float(target["x"]), float(target["y"])
            page.mouse.move(x, y)
            page.mouse.click(x, y)
            page.wait_for_timeout(250)
            page.mouse.wheel(0, 420)
            page.wait_for_timeout(300)
            expanded += 1
        except (KeyError, TypeError, ValueError):
            break
    return expanded


def visible_reply_control_labels(page: Any) -> list[str]:
    """Return short visible reply-control labels for pagination diagnostics."""
    try:
        labels = page.evaluate(
            """() => Array.from(document.querySelectorAll('button, [role="button"], span, a'))
              .map(el => String(el.innerText || el.textContent || '').replace(/\\s+/g, ''))
              .filter(text => text.includes('回复') && text.length <= 24)"""
        )
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for label in labels if isinstance(labels, list) else []:
        text = str(label or "")
        if text and len(text) <= 24 and text not in out:
            out.append(text)
    return out[:40]


def is_reply_expand_label(text: str) -> bool:
    return bool(REPLY_EXPAND_LABEL_RE.fullmatch(str(text or "").replace(" ", "")))


def should_expand_reply_label(previous_label: str, current_label: str) -> bool:
    return is_reply_expand_label(current_label) and str(previous_label or "") != str(current_label or "")


def _reply_parent_from_response_url(url: str) -> str:
    try:
        return str(parse_qs(urlparse(url).query).get("comment_id", [""])[0] or "")
    except Exception:
        return ""


def _has_auth_cookie(context: Any) -> bool:
    try:
        jar = {
            str(cookie.get("name") or ""): str(cookie.get("value") or "")
            for cookie in context.cookies("https://www.douyin.com")
        }
    except Exception:
        return False
    return short_video._has_douyin_login_cookie(jar) and browser_cookies.shared_status().get("has_login") is True


def _load_login_state() -> dict[str, Any]:
    data = _load_json(config.COMMENT_LEADS_LOGIN_STATE_JSON, {})
    return data if isinstance(data, dict) else {}


def _save_login_state(authenticated: bool, browser: str) -> None:
    _save_json(
        config.COMMENT_LEADS_LOGIN_STATE_JSON,
        {
            "authenticated": bool(authenticated),
            "browser": str(browser or ""),
            "verification_version": 2,
            "updated_at": _now(),
        },
    )


def _launch_comment_context(playwright: Any, profile: Path, *, headless: bool) -> tuple[Any, str]:
    """Use Edge for both product modules, with Chromium only as a local fallback."""
    base = {"headless": headless, "viewport": {"width": 1365, "height": 768}, "locale": "zh-CN"}
    for browser, extra in (("msedge", {"channel": "msedge"}), ("chromium", {})):
        try:
            return playwright.chromium.launch_persistent_context(str(profile), **base, **extra), browser
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("无法打开 Edge 或 Chromium 浏览器")


def _metadata_from_aweme_payload(data: dict[str, Any]) -> dict[str, str]:
    aweme = data.get("aweme_detail")
    if not aweme and isinstance(data.get("aweme_list"), list) and data["aweme_list"]:
        aweme = data["aweme_list"][0]
    if not isinstance(aweme, dict):
        return {}
    author = aweme.get("author") if isinstance(aweme.get("author"), dict) else {}
    avatar = ((author.get("avatar_thumb") or {}).get("url_list") or [""])[0] if isinstance(author, dict) else ""
    cover = ((aweme.get("video") or {}).get("cover") or {}).get("url_list") or []
    return {
        "author_name": str(author.get("nickname") or "").strip() if isinstance(author, dict) else "",
        "author_avatar": str(avatar or "").strip(),
        "author_sec_uid": str(author.get("sec_uid") or "").strip() if isinstance(author, dict) else "",
        "author_profile_url": _comment_profile_url(str(author.get("sec_uid") or "").strip()) if isinstance(author, dict) else "",
        "video_title": str(aweme.get("desc") or "").strip(),
        "video_cover": str((cover or [""])[0] or "").strip(),
    }


def _merge_metadata(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key in ("author_name", "author_avatar", "author_sec_uid", "author_profile_url", "video_title", "video_cover"):
        value = str(source.get(key) or "").strip()
        if value and not str(target.get(key) or "").strip():
            target[key] = value


def _read_page_metadata(page: Any) -> dict[str, str]:
    try:
        data = page.evaluate(
            """() => {
              const meta = sel => (document.querySelector(sel)?.content || '').trim();
              const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
              const userLinks = Array.from(document.querySelectorAll('a[href*="/user/"]'));
              const userLink = userLinks.find(a => clean(a.innerText || a.textContent)) || userLinks[0] || null;
              const userBox = userLink ? (userLink.closest('div') || userLink) : null;
              const avatarImg = userBox ? userBox.querySelector('img') : null;
              const title = clean(meta('meta[property="og:title"]') || document.title);
              const cover = clean(meta('meta[property="og:image"]') || meta('meta[name="twitter:image"]'));
              return {
                author_name: userLink ? clean(userLink.innerText || userLink.textContent).split(' ')[0] : '',
                author_avatar: avatarImg ? clean(avatarImg.currentSrc || avatarImg.src || avatarImg.getAttribute('src')) : '',
                author_profile_url: userLink ? clean(userLink.href || userLink.getAttribute('href')) : '',
                video_title: title,
                video_cover: cover
              };
            }"""
        )
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    author_profile_url = str(data.get("author_profile_url") or "")
    sec_uid = extract_author_sec_uid(author_profile_url)
    return {
        "author_name": str(data.get("author_name") or "").strip(),
        "author_avatar": str(data.get("author_avatar") or "").strip(),
        "author_sec_uid": sec_uid,
        "author_profile_url": author_profile_url,
        "video_title": str(data.get("video_title") or "").strip(),
        "video_cover": str(data.get("video_cover") or "").strip(),
    }


def login_status() -> dict[str, Any]:
    profile_dir = config.COMMENT_LEADS_PROFILE_DIR
    has_profile = profile_dir.exists() and any(profile_dir.iterdir())
    shared = browser_cookies.shared_status()
    return {
        "profile_dir": str(profile_dir),
        "has_profile": bool(has_profile),
        "logged_in": bool(shared.get("has_login")),
        "browser": str(shared.get("browser") or "msedge"),
        "cookie_count": int(shared.get("cookie_count") or 0),
    }


def open_login_browser(*, start_url: str = "https://www.douyin.com/", wait_ms: int = 30000) -> dict[str, Any]:
    result = short_video.remint_short_video_cookie(
        start_url or "https://www.douyin.com/", timeout_sec=max(30, min(wait_ms // 1000, 180))
    )
    return {"ok": bool(result.get("ok")) and bool(result.get("has_login")), "message": result.get("message") or "抖音授权完成"}


def capture_video_comments(url: str, *, max_comments: int = 100, headed: bool = True) -> CaptureResult:
    """Capture public comments from one Douyin video using an authorized browser profile."""
    source_url = extract_first_url(url)
    aweme_id = extract_aweme_id(source_url)
    source_url = _canonical_video_url(source_url, aweme_id)
    if not source_url:
        return CaptureResult(False, [], {}, "缺少视频链接")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return CaptureResult(False, [], {}, f"Playwright 未安装：{exc}")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    metadata: dict[str, Any] = {
        "source_url": source_url,
        "aweme_id": aweme_id,
        "authenticated": False,
        "comment_response_count": 0,
        "reply_response_count": 0,
        "reported_total": None,
        "started_at": _now(),
    }
    _merge_metadata(metadata, _profile_cache_metadata(extract_author_sec_uid(url)))
    profile = config.COMMENT_LEADS_PROFILE_DIR
    profile.mkdir(parents=True, exist_ok=True)
    started_wall = time.time()
    last_new_wall = started_wall
    last_response_wall = 0.0
    max_capture_seconds = min(120, max(45, 20 + int(max_comments / 12)))
    reply_next_requests: dict[str, tuple[str, int]] = {}
    requested_reply_pages: set[tuple[str, int]] = set()

    with sync_playwright() as p:
        context, browser = _launch_comment_context(p, profile, headless=not headed)
        shared_jar = browser_cookies.cached_jar() if browser_cookies.shared_status().get("has_login") else {}
        if shared_jar:
            context.add_cookies([
                {"name": k, "value": v, "domain": ".douyin.com", "path": "/"}
                for k, v in shared_jar.items()
            ])
        page = context.new_page()

        def on_response(resp: Any) -> None:
            nonlocal last_new_wall, last_response_wall
            if "/aweme/v1/web/comment/list" not in resp.url:
                if "/aweme/v1/web/aweme/detail" in resp.url or "/aweme/v1/web/aweme/post" in resp.url:
                    try:
                        _merge_metadata(metadata, _metadata_from_aweme_payload(resp.json()))
                    except Exception:
                        return
                return
            try:
                data = resp.json()
            except Exception:
                return
            is_reply_response = "/comment/list/reply" in resp.url
            parent_comment_id = _reply_parent_from_response_url(resp.url) if is_reply_response else ""
            last_response_wall = time.time()
            metadata["comment_response_count"] += 1
            if is_reply_response:
                metadata["reply_response_count"] += 1
            else:
                metadata["reported_total"] = data.get("total")
                metadata["has_more"] = data.get("has_more")
                metadata["cursor"] = data.get("cursor")
            raw_comments = data.get("comments") or []
            if is_reply_response:
                reply_pages = metadata.setdefault("reply_pages", [])
                if isinstance(reply_pages, list) and len(reply_pages) < 100:
                    reply_pages.append(reply_page_summary(parent_comment_id, data, raw_comments))
                if data.get("has_more") in (True, 1, "1") and parent_comment_id:
                    try:
                        reply_next_requests[parent_comment_id] = (resp.url, int(data.get("cursor") or 0))
                    except (TypeError, ValueError):
                        pass
            if not is_reply_response:
                stats = reply_statistics(raw_comments)
                metadata["reported_reply_total"] = int(metadata.get("reported_reply_total") or 0) + stats["reported"]
                metadata["embedded_reply_count"] = int(metadata.get("embedded_reply_count") or 0) + stats["embedded"]
                pending_ids = metadata.setdefault("pending_reply_parent_ids", [])
                if isinstance(pending_ids, list):
                    for parent_id in stats["parent_ids"]:
                        if parent_id not in pending_ids and len(pending_ids) < 100:
                            pending_ids.append(parent_id)
            normalized = [
                row
                for comment in raw_comments
                if isinstance(comment, dict)
                for row in normalize_comment_tree(
                    comment,
                    aweme_id=aweme_id,
                    source_url=source_url,
                    parent_comment_id=parent_comment_id,
                    level=2 if is_reply_response else 1,
                )
            ]
            before_count = len(rows)
            _append_unique_rows(rows, normalized, seen_ids, max_comments)
            added_count = len(rows) - before_count
            metadata["last_response_rows"] = len(raw_comments)
            metadata["captured_so_far"] = len(rows)
            if added_count > 0:
                last_new_wall = last_response_wall
                metadata["empty_response_streak"] = 0
            else:
                metadata["empty_response_streak"] = int(metadata.get("empty_response_streak") or 0) + 1
            try:
                reported_total = int(metadata.get("reported_total") or 0)
            except (TypeError, ValueError):
                reported_total = 0
            if not is_reply_response and reported_total == 0 and not raw_comments:
                metadata["comment_no_more"] = True
            if reported_total and len(rows) >= min(reported_total, max_comments):
                metadata["reached_reported_total"] = True
            if not is_reply_response and data.get("has_more") in (False, 0, "0"):
                metadata["comment_no_more"] = True

        page.on("response", on_response)
        page.goto(source_url, wait_until="domcontentloaded", timeout=30000)
        try:
            metadata["page_title"] = page.title()
        except Exception:
            pass
        page.wait_for_timeout(4500)
        _merge_metadata(metadata, _read_page_metadata(page))
        metadata["authenticated"] = _has_auth_cookie(context)
        if not metadata["authenticated"]:
            _save_login_state(False, browser)
            context.close()
            return CaptureResult(False, rows, metadata, "未登录抖音，请先点击“授权登录”")
        _save_login_state(True, browser)

        for selector in ('[aria-label*=评论]', '[title*=评论]', 'button:has-text("评论")', "text=评论"):
            if rows:
                break
            try:
                locator = page.locator(selector).first
                if locator.count():
                    locator.click(timeout=2500)
                    page.wait_for_timeout(3500)
            except Exception:
                continue

        max_scroll_attempts = min(240, max(75, int(max_comments / 4)))
        for _ in range(max_scroll_attempts):
            if len(rows) >= max_comments:
                break
            elapsed = time.time() - started_wall
            if metadata.get("reached_reported_total"):
                break
            if metadata.get("comment_response_count") and rows and time.time() - last_new_wall > 15:
                metadata["stalled_without_new_comments"] = True
                break
            if (
                metadata.get("comment_response_count")
                and not rows
                and int(metadata.get("empty_response_streak") or 0) >= 3
                and time.time() - last_response_wall > 2
            ):
                metadata["empty_comment_responses"] = True
                break
            if elapsed > max_capture_seconds:
                metadata["timeout"] = True
                break
            if elapsed > 25 and not metadata.get("comment_response_count"):
                metadata["no_comment_api"] = True
                break
            try:
                metadata["reply_expand_attempts"] = int(metadata.get("reply_expand_attempts") or 0) + _expand_comment_replies(page)
            except Exception:
                pass
            _scroll_comment_panel(page)
            for parent_id, (response_url, next_cursor) in list(reply_next_requests.items()):
                page_key = (parent_id, next_cursor)
                if page_key in requested_reply_pages:
                    continue
                requested_reply_pages.add(page_key)
                next_url = reply_next_page_url(response_url, next_cursor)
                try:
                    result = page.evaluate(
                        """async url => {
                          try {
                            const response = await fetch(url, {credentials: 'include'});
                            return {ok: response.ok, status: response.status};
                          } catch (error) {
                            return {ok: false, status: 0};
                          }
                        }""",
                        next_url,
                    )
                    metadata["reply_direct_page_requests"] = int(metadata.get("reply_direct_page_requests") or 0) + 1
                    if not isinstance(result, dict) or not result.get("ok"):
                        metadata["reply_direct_page_failed"] = True
                except Exception:
                    metadata["reply_direct_page_failed"] = True
            page.wait_for_timeout(700)

        deadline = time.time() + 3
        while len(rows) < max_comments and time.time() < deadline:
            page.wait_for_timeout(1000)
        metadata["reply_control_labels"] = visible_reply_control_labels(page)
        context.close()

    metadata["finished_at"] = _now()
    if rows:
        return CaptureResult(True, rows[:max_comments], metadata, "")
    if metadata.get("empty_comment_responses") or (
        metadata.get("comment_response_count") and not metadata.get("reported_total")
    ):
        return CaptureResult(False, [], metadata, "评论接口已返回，但该作品暂无可采集评论")
    if metadata.get("no_comment_api"):
        return CaptureResult(False, [], metadata, "没有捕获到评论接口，请确认链接是具体作品，并且评论区可见")
    if metadata.get("timeout"):
        return CaptureResult(False, [], metadata, "采集超时，可能页面加载慢或评论区被限制")
    return CaptureResult(False, [], metadata, "没有采集到评论，可能评论区未展开或登录态失效")


def ingest_rows(rows: list[dict[str, Any]], *, monitor_id: str = "") -> dict[str, Any]:
    store = load_store()
    existing = {str(r.get("comment_id") or "") for r in store["leads"] if r.get("comment_id")}
    inserted = 0
    for row in rows:
        cid = str(row.get("comment_id") or "")
        if cid and cid in existing:
            continue
        item = dict(row)
        item["lead_id"] = cid or f"lead_{_now()}_{inserted}"
        item["monitor_id"] = monitor_id
        store["leads"].insert(0, item)
        if cid:
            existing.add(cid)
        inserted += 1
    save_store(store)
    return {"inserted": inserted, "total": len(store["leads"])}


def _update_monitor_after_run(
    store: dict[str, Any],
    monitor_id: str,
    *,
    last_count: int,
    error: str,
    metadata: dict[str, Any],
) -> None:
    monitor = next((m for m in store["monitors"] if m.get("id") == monitor_id), None)
    if not monitor:
        return
    monitor["last_run_at"] = _now()
    monitor["last_error"] = error
    monitor["last_count"] = last_count
    if metadata.get("reported_total") is not None:
        try:
            monitor["last_reported_total"] = int(metadata.get("reported_total") or 0)
        except (TypeError, ValueError):
            monitor["last_reported_total"] = 0
    _merge_metadata(monitor, metadata)
    if monitor.get("author_name"):
        monitor["title"] = monitor["author_name"]
    elif monitor.get("video_title"):
        monitor["title"] = monitor["video_title"]


def _profile_monitor_metadata(profile: dict[str, Any], videos: list[dict[str, Any]]) -> dict[str, Any]:
    sec_uid = str(profile.get("sec_user_id") or "").strip()
    return {
        "author_sec_uid": sec_uid,
        "author_name": str(profile.get("nickname") or "").strip(),
        "author_avatar": str(profile.get("avatar_url") or "").strip(),
        "author_profile_url": f"https://www.douyin.com/user/{sec_uid}" if sec_uid else "",
        "video_title": f"最近 {len(videos)} 条作品评论监控" if videos else "",
        "discovered_video_count": len(videos),
    }


def resolve_profile_works(url: str, *, owner: str = "", max_comments: int = 100, max_videos: int = 5) -> dict[str, Any]:
    """Resolve a monitored homepage into visible works without collecting comments."""
    monitor = add_monitor(url, owner=owner, max_comments=max_comments, max_videos=max_videos)
    if monitor.get("target_type") != "profile":
        return {
            "ok": True,
            "monitor": monitor,
            "profile": {},
            "videos": [{
                "id": monitor.get("aweme_id") or monitor.get("id"),
                "url": monitor.get("target_url"),
                "title": monitor.get("video_title") or monitor.get("title") or "指定作品",
                "cover_url": monitor.get("video_cover") or "",
                "like_count": None,
                "selected": True,
            }],
            "warning": "",
        }
    recent_count = max(1, min(int(max_videos or 5), 50))
    resolved = short_video.resolve_profile(str(monitor.get("target_url") or monitor.get("raw_url") or ""), recent_count=recent_count)
    profile = resolved.get("profile") if isinstance(resolved.get("profile"), dict) else {}
    videos = [v for v in (resolved.get("videos") or []) if isinstance(v, dict)]
    metadata = _profile_monitor_metadata(profile, videos)
    store = load_store()
    _update_monitor_after_run(store, str(monitor.get("id") or ""), last_count=int(monitor.get("last_count") or 0), error="", metadata=metadata)
    save_store(store)
    monitor = next((m for m in load_store().get("monitors", []) if m.get("id") == monitor.get("id")), monitor)
    return {
        "ok": True,
        "monitor": monitor,
        "profile": profile,
        "videos": videos,
        "warning": resolved.get("warning", ""),
    }


def run_selected_videos(
    monitor_id: str,
    videos: list[dict[str, Any]],
    *,
    max_comments: int | None = None,
) -> dict[str, Any]:
    """Collect comments for explicit selected works under one monitor."""
    store = load_store()
    monitor = next((m for m in store["monitors"] if m.get("id") == monitor_id), None)
    if not monitor:
        raise ValueError("监控对象不存在")
    clean_videos = []
    for item in videos[:50]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        aweme_id = extract_aweme_id(url) or str(item.get("id") or "").strip()
        if not url and aweme_id:
            url = _canonical_video_url("", aweme_id)
        if url:
            clean_videos.append({**item, "url": url, "id": aweme_id or str(item.get("id") or "")})
    if not clean_videos:
        raise ValueError("请先选择要采集评论的作品")

    per_video_limit = max(1, min(int(max_comments or monitor.get("max_comments") or 100), 2000))
    total_captured = 0
    total_inserted = 0
    reported_total = 0
    errors: list[str] = []
    metadata: dict[str, Any] = {
        "target_type": "selected_videos",
        "selected_video_count": len(clean_videos),
        "videos": [
            {
                "id": v.get("id", ""),
                "title": v.get("title", ""),
                "url": v.get("url", ""),
                "cover_url": v.get("cover_url", ""),
            }
            for v in clean_videos
        ],
    }
    for video in clean_videos:
        result = capture_video_comments(str(video.get("url") or ""), max_comments=per_video_limit)
        total_captured += len(result.rows)
        try:
            reported_total += int(result.metadata.get("reported_total") or 0)
        except (TypeError, ValueError):
            pass
        _merge_metadata(metadata, result.metadata)
        if result.rows:
            inserted = ingest_rows(result.rows, monitor_id=monitor_id)
            total_inserted += int(inserted.get("inserted") or 0)
        elif result.error:
            errors.append(f"{video.get('title') or video.get('id') or '作品'}：{result.error}")

    store = load_store()
    metadata["reported_total"] = reported_total
    error = "；".join(dict.fromkeys(errors))[:500]
    ok = total_captured > 0
    job = {
        "job_id": f"job_{_now()}",
        "monitor_id": monitor_id,
        "ok": ok,
        "error": error,
        "metadata": metadata,
        "captured": total_captured,
        "created_at": _now(),
    }
    _update_monitor_after_run(store, monitor_id, last_count=total_captured, error=error, metadata=metadata)
    store["jobs"].insert(0, job)
    store["jobs"] = store["jobs"][:100]
    save_store(store)
    return {
        "ok": ok,
        "error": error,
        "captured": total_captured,
        "inserted": total_inserted,
        "total": len(store.get("leads", [])),
        "metadata": metadata,
    }


def run_monitor(monitor_id: str) -> dict[str, Any]:
    store = load_store()
    monitor = next((m for m in store["monitors"] if m.get("id") == monitor_id), None)
    if not monitor:
        raise ValueError("监控对象不存在")
    if monitor.get("target_type") == "profile":
        max_videos = max(1, min(int(monitor.get("max_videos") or 5), 50))
        max_comments = max(1, min(int(monitor.get("max_comments") or 100), 2000))
        try:
            resolved = short_video.resolve_profile(str(monitor.get("target_url") or monitor.get("raw_url") or ""), recent_count=max_videos)
        except Exception as exc:  # noqa: BLE001
            error = f"主页作品读取失败：{type(exc).__name__}: {exc}"
            job = {
                "job_id": f"job_{_now()}",
                "monitor_id": monitor_id,
                "ok": False,
                "error": error,
                "metadata": {"target_type": "profile"},
                "captured": 0,
                "created_at": _now(),
            }
            store["jobs"].insert(0, job)
            _update_monitor_after_run(store, monitor_id, last_count=0, error=error, metadata={})
            save_store(store)
            return {"ok": False, "error": error, "captured": 0, "inserted": 0, "total": len(store.get("leads", [])), "metadata": job["metadata"]}
        profile = resolved.get("profile") if isinstance(resolved.get("profile"), dict) else {}
        videos = [v for v in (resolved.get("videos") or []) if isinstance(v, dict)]
        metadata = _profile_monitor_metadata(profile, videos)
        metadata["target_type"] = "profile"
        metadata["profile_warning"] = resolved.get("warning", "")
        total_captured = 0
        total_inserted = 0
        errors: list[str] = []
        for video in videos[:max_videos]:
            video_url = str(video.get("url") or "").strip()
            if not video_url:
                continue
            result = capture_video_comments(video_url, max_comments=max_comments)
            total_captured += len(result.rows)
            _merge_metadata(metadata, result.metadata)
            if result.rows:
                inserted = ingest_rows(result.rows, monitor_id=monitor_id)
                total_inserted += int(inserted.get("inserted") or 0)
                store = load_store()
            elif result.error:
                errors.append(result.error)
        if not videos:
            errors.append("未读取到账号作品，请先在短视频中心完成账号授权或稍后重试")
        error = "；".join(dict.fromkeys(errors))[:500]
        ok = total_captured > 0
        job = {
            "job_id": f"job_{_now()}",
            "monitor_id": monitor_id,
            "ok": ok,
            "error": error,
            "metadata": metadata,
            "captured": total_captured,
            "created_at": _now(),
        }
        store = load_store()
        _update_monitor_after_run(store, monitor_id, last_count=total_captured, error=error, metadata=metadata)
        store["jobs"].insert(0, job)
        store["jobs"] = store["jobs"][:100]
        save_store(store)
        return {"ok": ok, "error": error, "captured": total_captured, "inserted": total_inserted, "total": len(store.get("leads", [])), "metadata": metadata}

    result = capture_video_comments(monitor["target_url"], max_comments=int(monitor.get("max_comments") or 100))
    job = {
        "job_id": f"job_{_now()}",
        "monitor_id": monitor_id,
        "ok": result.ok,
        "error": result.error,
        "metadata": result.metadata,
        "captured": len(result.rows),
        "created_at": _now(),
    }
    inserted = {"inserted": 0, "total": len(store.get("leads", []))}
    if result.rows:
        inserted = ingest_rows(result.rows, monitor_id=monitor_id)
        store = load_store()
    _update_monitor_after_run(store, monitor_id, last_count=len(result.rows), error=result.error, metadata=result.metadata)
    store["jobs"].insert(0, job)
    store["jobs"] = store["jobs"][:100]
    save_store(store)
    return {"ok": result.ok, "error": result.error, "captured": len(result.rows), **inserted, "metadata": result.metadata}


def export_leads_csv(rows: list[dict[str, Any]] | None = None) -> Path:
    rows = list_leads(limit=5000) if rows is None else rows
    out = config.COMMENT_LEADS_EXPORT_DIR / f"comment_leads_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return out
