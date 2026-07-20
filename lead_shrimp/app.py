"""Standalone, localhost-only Comment Lead Radar desktop application.

The collector is shared with LiveWatch, but this app has its own UI, data
directory, product code and card-key entitlement boundary.  It never exposes
the live replay pages or their APIs.
"""

from __future__ import annotations

import argparse
import multiprocessing
import os
import pickle
import queue
import threading
import time
from pathlib import Path
from typing import Any

# Must happen before any pipeline module imports config.
os.environ.setdefault("LEADSHRIMP_STANDALONE", "1")
if not os.environ.get("LEADSHRIMP_DATA_DIR"):
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    os.environ["LEADSHRIMP_DATA_DIR"] = str(local_app_data / "LeadShrimp" / "data")
# These are public deployment values only.  The matching Ed25519 private key,
# card database and administrator token remain on the licensing server.
os.environ.setdefault("LEADSHRIMP_LICENSE_SERVER_URL", "https://license.runmo.art")
os.environ.setdefault("LEADSHRIMP_LICENSE_PUBLIC_KEY", "YYHkNVmcsiWjoYweNOa7CEBP3WGRyBbB6Cf3_qvQchc")

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from pipeline import comment_leads, config, lead_ai, license_client, license_manager
from pipeline.web_security import LOCAL_UI_ORIGIN_REGEX

PRODUCT_CODE = "lead_shrimp"
_ROOT = Path(__file__).resolve().parent
app = FastAPI(title="获客虾", version="0.1.0", docs_url=None, redoc_url=None)
_internal_scheduler_started = False
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=LOCAL_UI_ORIGIN_REGEX,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["content-type"],
)


def frontend_path() -> Path:
    packaged_assets = Path(os.environ.get("LEADSHRIMP_ASSET_DIR") or "").expanduser()
    candidate = packaged_assets / "frontend.html" if str(packaged_assets) else None
    if candidate and candidate.is_file():
        return candidate
    return _ROOT / "frontend.html"


def required_feature_for_path(path: str) -> str | None:
    target = (path or "").rstrip("/") or "/"
    if target == "/api/comment-leads/export":
        return "export"
    if target.startswith("/api/comment-leads/") and target not in {
        "/api/comment-leads/status",
        "/api/comment-leads/login",
    }:
        return "lead_radar"
    return None


def _child_call(result_queue: Any, fn: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    try:
        result_queue.put(("ok", fn(*args, **kwargs)))
    except BaseException as exc:  # noqa: BLE001 - serialize child-process browser failures.
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _run_blocking(fn, *args, _process_timeout_sec: float | None = 240, **kwargs):
    """Run Playwright work in a child process so sync API never meets FastAPI's event loop."""
    try:
        pickle.dumps(fn)
    except (AttributeError, pickle.PickleError):
        return fn(*args, **kwargs)
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_child_call, args=(result_queue, fn, args, kwargs))
    proc.start()
    proc.join(_process_timeout_sec)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        raise RuntimeError("浏览器任务超时，请稍后重试")
    try:
        status, *payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise RuntimeError("浏览器任务异常退出，请重新打开软件后重试") from exc
    if status == "ok":
        return payload[0]
    error_type, message = payload
    raise RuntimeError(f"{error_type}: {message}".strip())


def _bounded_int(value: object, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value or default), maximum))
    except (TypeError, ValueError):
        return default


def _internal_scheduler_loop() -> None:
    while True:
        try:
            comment_leads.run_due_internal_daily_scan()
        except Exception:
            pass
        time.sleep(60)


@app.on_event("startup")
def start_internal_scheduler() -> None:
    global _internal_scheduler_started
    if not _internal_scheduler_started:
        threading.Thread(target=_internal_scheduler_loop, name="LeadShrimpInternalScheduler", daemon=True).start()
        _internal_scheduler_started = True


@app.middleware("http")
async def _license_gate(request: Request, call_next):
    feature = required_feature_for_path(request.url.path)
    if feature:
        try:
            license_manager.require_feature(feature)
        except license_manager.LicenseFeatureError as exc:
            return JSONResponse(status_code=403, content={"detail": str(exc)})
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return frontend_path().read_text(encoding="utf-8")


@app.get("/api/health")
def api_health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "lead_shrimp"})


@app.get("/api/license/status")
def api_license_status() -> JSONResponse:
    status = license_manager.public_status()
    # Development mode intentionally keeps local workflow testable, but it is
    # not a customer entitlement and must never be rendered as an activation.
    card_active = bool(status.get("ok")) and str(status.get("mode") or "") in {"licensed", "grace"}
    license_required = bool(status.get("enforced"))
    return JSONResponse(
        {
            "ok": True,
            "product_code": PRODUCT_CODE,
            "licensed": card_active,
            "license_required": license_required,
            **status,
        }
    )


@app.post("/api/license/activate")
def api_activate_card(payload: dict[str, object] = Body(...)) -> JSONResponse:
    card_key = str(payload.get("card_key") or "").strip()
    if not card_key:
        raise HTTPException(status_code=400, detail="请输入卡密")
    try:
        status = license_client.activate_card_key(card_key)
    except license_client.LicenseClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "product_code": PRODUCT_CODE, "licensed": True, **status})


@app.post("/api/license/refresh")
def api_refresh_card() -> JSONResponse:
    try:
        status = license_client.refresh_license()
    except license_client.LicenseClientError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    card_active = bool(status.get("ok")) and str(status.get("mode") or "") in {"licensed", "grace"}
    return JSONResponse({"ok": True, "product_code": PRODUCT_CODE, "licensed": card_active, **status})


@app.get("/api/comment-leads/status")
def api_comment_leads_status() -> JSONResponse:
    return JSONResponse({"ok": True, **comment_leads.login_status()})


@app.post("/api/comment-leads/login")
def api_comment_leads_login(payload: dict[str, object] = Body(default={})) -> JSONResponse:
    current = comment_leads.login_status()
    if current.get("logged_in"):
        return JSONResponse({
            "ok": True,
            "already_logged_in": True,
            "has_login": True,
            "browser": current.get("browser", "msedge"),
            "cookie_count": current.get("cookie_count", 0),
            "expires_at": current.get("expires_at", 0),
            "message": "抖音已登录，无需重复打开登录窗口。",
        })
    start_url = str(payload.get("start_url") or "https://www.douyin.com/").strip()
    wait_ms = _bounded_int(payload.get("wait_ms"), default=0, minimum=0, maximum=180000)
    result = _run_blocking(
        comment_leads.open_login_browser,
        start_url=start_url,
        wait_ms=wait_ms,
        _process_timeout_sec=None,
    )
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


@app.get("/api/comment-leads/monitors")
def api_comment_leads_monitors() -> JSONResponse:
    return JSONResponse({"ok": True, "monitors": comment_leads.list_monitors()})


@app.delete("/api/comment-leads/monitors/{monitor_id}")
def api_comment_leads_delete_monitor(monitor_id: str) -> JSONResponse:
    clean_id = str(monitor_id or "").strip()
    if not clean_id:
        raise HTTPException(status_code=400, detail="缺少监控对象 ID")
    if not comment_leads.delete_monitor(clean_id):
        raise HTTPException(status_code=404, detail="监控对象不存在或已删除")
    return JSONResponse({"ok": True, "deleted": True, "monitor_id": clean_id})


@app.get("/api/comment-leads/diagnosis")
def api_comment_leads_diagnosis(monitor_id: str = Query(default="", max_length=256)) -> JSONResponse:
    """Return concise, user-facing recovery guidance for the latest collection."""
    login = comment_leads.login_status()
    monitors = comment_leads.list_monitors()
    monitor = next((row for row in monitors if str(row.get("id") or "") == monitor_id), None) if monitor_id else (monitors[0] if monitors else None)
    if not monitor:
        return JSONResponse({
            "ok": True,
            "title": "等待选择监控对象",
            "detail": "添加抖音主页或视频链接后，系统会在这里给出采集检查结果。",
            "next_action": "monitor",
            "action_label": "",
            "captured": 0,
            "logged_in": bool(login.get("logged_in")),
            "checks": [{"label": "抖音登录", "ok": bool(login.get("logged_in")), "detail": "登录态可用" if login.get("logged_in") else "尚未登录"}],
        })

    captured = max(0, int(monitor.get("last_count") or 0))
    last_error = str(monitor.get("last_error") or "").strip()
    logged_in = bool(login.get("logged_in"))
    if not logged_in or "未登录" in last_error:
        title = "需要完成抖音登录"
        detail = f"已保存 {captured} 条已采集评论。登录后可继续补采剩余作品。" if captured else "当前浏览器没有可用抖音登录态，无法继续读取评论。"
        next_action, action_label = "login", "打开登录窗口"
    elif last_error:
        title, detail, next_action, action_label = "采集遇到可恢复问题", last_error, "retry", "重新采集"
    elif captured:
        title, detail, next_action, action_label = "最近一次采集正常", f"最近一次已采集 {captured} 条评论。", "", ""
    else:
        title, detail, next_action, action_label = "暂未读到评论", "请确认链接有效、作品公开可见，再重新采集。", "retry", "重新采集"
    return JSONResponse({
        "ok": True,
        "title": title,
        "detail": detail,
        "next_action": next_action,
        "action_label": action_label,
        "captured": captured,
        "logged_in": logged_in,
        "checks": [
            {"label": "抖音登录", "ok": logged_in, "detail": "登录态可用" if logged_in else "尚未登录"},
            {"label": "本次评论", "ok": captured > 0, "detail": f"已保存 {captured} 条" if captured else "尚未采集到评论"},
        ],
    })


@app.post("/api/comment-leads/monitors")
def api_comment_leads_add_monitor(payload: dict[str, object] = Body(...)) -> JSONResponse:
    try:
        monitor = comment_leads.add_monitor(
            str(payload.get("url") or ""),
            title=str(payload.get("title") or ""),
            owner=str(payload.get("owner") or ""),
            max_comments=_bounded_int(payload.get("max_comments"), default=500, minimum=1, maximum=2000),
            max_videos=_bounded_int(payload.get("max_videos"), default=5, minimum=1, maximum=20),
            force=bool(payload.get("force")),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "monitor": monitor})


@app.get("/api/comment-leads/internal-test/overview")
def api_internal_test_overview() -> JSONResponse:
    """Read-only preview for experimental AI automation; production flow is untouched."""
    return JSONResponse(comment_leads.list_internal_test_overview())


@app.post("/api/comment-leads/internal-test/run-daily-scan")
def api_internal_test_daily_scan() -> JSONResponse:
    return JSONResponse(_run_blocking(comment_leads.run_internal_daily_scan))


@app.post("/api/comment-leads/internal-test/schedule")
def api_internal_test_schedule(payload: dict[str, object] = Body(...)) -> JSONResponse:
    try:
        return JSONResponse(comment_leads.save_internal_test_schedule(enabled=bool(payload.get("enabled")), run_time=str(payload.get("time") or "08:00")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/comment-leads/internal-test/analyze")
def api_internal_test_analyze(payload: dict[str, object] = Body(...)) -> JSONResponse:
    lead_ids = payload.get("lead_ids") if isinstance(payload.get("lead_ids"), list) else []
    context = payload.get("business_context") if isinstance(payload.get("business_context"), dict) else {}
    try:
        return JSONResponse(lead_ai.analyze_stored_leads([str(value or "") for value in lead_ids], business_context=context))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/comment-leads/profile-videos")
def api_comment_leads_profile_videos(payload: dict[str, object] = Body(...)) -> JSONResponse:
    try:
        result = _run_blocking(
            comment_leads.resolve_profile_works,
            str(payload.get("url") or ""),
            owner=str(payload.get("owner") or ""),
            max_comments=_bounded_int(payload.get("max_comments"), default=500, minimum=1, maximum=2000),
            max_videos=_bounded_int(payload.get("max_videos"), default=5, minimum=1, maximum=20),
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - browser/collector errors must remain JSON for the desktop UI.
        message = str(exc).strip() or "采集进程异常退出，请重新登录后重试"
        raise HTTPException(status_code=503, detail=f"评论采集失败：{message[:240]}") from exc
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


@app.get("/api/comment-leads/leads")
def api_comment_leads_list(
    status: str = Query(default="", max_length=64),
    keyword: str = Query(default="", max_length=128),
    limit: int = Query(default=5000, ge=1, le=5000),
) -> JSONResponse:
    return JSONResponse({"ok": True, "leads": comment_leads.list_leads(status=status, keyword=keyword, limit=limit)})


@app.post("/api/comment-leads/run")
def api_comment_leads_run(payload: dict[str, object] = Body(...)) -> JSONResponse:
    selected_videos = payload.get("videos")
    monitor_id = str(payload.get("monitor_id") or "").strip()
    max_comments = _bounded_int(payload.get("max_comments"), default=500, minimum=1, maximum=2000)
    try:
        if monitor_id and isinstance(selected_videos, list):
            result = _run_blocking(comment_leads.run_selected_videos, monitor_id, selected_videos[:50], max_comments=max_comments)
        elif monitor_id:
            result = _run_blocking(comment_leads.run_monitor, monitor_id)
        else:
            monitor = comment_leads.add_monitor(
                str(payload.get("url") or ""),
                title=str(payload.get("title") or ""),
                owner=str(payload.get("owner") or ""),
                max_comments=max_comments,
                max_videos=_bounded_int(payload.get("max_videos"), default=5, minimum=1, maximum=20),
            )
            result = _run_blocking(comment_leads.run_monitor, str(monitor["id"]))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - browser/collector errors must remain JSON for the desktop UI.
        message = str(exc).strip() or "采集进程异常退出，请重新登录后重试"
        raise HTTPException(status_code=503, detail=f"评论采集失败：{message[:240]}") from exc
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


@app.get("/api/comment-leads/export")
def api_comment_leads_export() -> FileResponse:
    out = comment_leads.export_leads_xlsx()
    return FileResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=out.name)


@app.post("/api/comment-leads/export")
def api_comment_leads_export_selected(payload: dict[str, object] = Body(...)) -> FileResponse:
    raw_ids = payload.get("lead_ids")
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="请选择要导出的线索")
    lead_ids = [str(value or "").strip() for value in raw_ids if str(value or "").strip()]
    if not lead_ids:
        raise HTTPException(status_code=400, detail="请选择要导出的线索")
    rows = comment_leads.list_leads_by_ids(lead_ids)
    if not rows:
        raise HTTPException(status_code=404, detail="选中的线索已不存在，请刷新数据后重试")
    out = comment_leads.export_leads_xlsx(rows)
    return FileResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", filename=out.name)


def main() -> int:
    parser = argparse.ArgumentParser(description="获客虾本地控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8922)
    args = parser.parse_args()
    config.ensure_dirs()
    # pythonw.exe has no stdout/stderr streams. Uvicorn's default formatter
    # calls sys.stdout.isatty(), which crashes before binding the service.
    uvicorn.run(
        app,
        host=args.host,
        port=max(1, min(args.port, 65535)),
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
