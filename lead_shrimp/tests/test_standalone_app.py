from __future__ import annotations

from fastapi.testclient import TestClient


def test_standalone_app_has_a_dedicated_product_boundary() -> None:
    from lead_shrimp.app import PRODUCT_CODE, app, required_feature_for_path

    assert app.title == "获客虾"
    assert PRODUCT_CODE == "lead_shrimp"
    assert required_feature_for_path("/api/comment-leads/run") == "lead_radar"
    assert required_feature_for_path("/api/comment-leads/export") == "export"
    assert required_feature_for_path("/api/license/activate") is None


def test_license_status_is_available_before_card_activation() -> None:
    from lead_shrimp.app import app

    response = TestClient(app).get("/api/license/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["product_code"] == "lead_shrimp"
    assert "licensed" in payload
    assert payload["licensed"] is False
    assert payload["license_required"] is False


def test_health_endpoint_is_independent_from_license_status() -> None:
    from lead_shrimp.app import app

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "lead_shrimp"}


def test_frontend_does_not_expose_replay_navigation() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert "AI 获客系统" not in page
    assert "评论线索工作台" in page
    assert "/api/license/activate" in page


def test_launcher_removes_its_browser_only_flag_before_starting_api() -> None:
    from lead_shrimp.launcher import app_argv

    assert app_argv(["LeadShrimpLauncher.exe", "--port", "8922", "--no-browser"]) == [
        "LeadShrimpLauncher.exe",
        "--port",
        "8922",
    ]


def test_launcher_rewrites_the_requested_port_for_the_selected_free_port() -> None:
    from lead_shrimp.launcher import with_port

    assert with_port(["LeadShrimpLauncher.exe", "--port", "8922"], 8931) == [
        "LeadShrimpLauncher.exe",
        "--port",
        "8931",
    ]
    assert with_port(["LeadShrimpLauncher.exe"], 8931) == [
        "LeadShrimpLauncher.exe",
        "--port",
        "8931",
    ]


def test_launcher_falls_back_when_the_preferred_port_is_busy(monkeypatch) -> None:
    from lead_shrimp import launcher

    attempts: list[int] = []

    def fake_bind(port: int) -> bool:
        attempts.append(port)
        return port != 8922

    monkeypatch.setattr(launcher, "_port_is_free", fake_bind)

    assert launcher.choose_port(8922, attempts=3) == 8923
    assert attempts == [8922, 8923]


def test_launcher_waits_for_the_health_endpoint_before_opening_browser(monkeypatch) -> None:
    from lead_shrimp import launcher

    opened: list[str] = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url, new=0: opened.append(url) or True)
    responses = iter([False, True])
    monkeypatch.setattr(launcher, "service_ready", lambda _port: next(responses))

    assert launcher.wait_for_service_and_open(8931, timeout_sec=1, poll_sec=0) is True
    assert opened == ["http://127.0.0.1:8931/"]


def test_launcher_health_probe_uses_the_dedicated_health_endpoint() -> None:
    from lead_shrimp import launcher

    source = __import__("inspect").getsource(launcher.service_ready)

    assert "/api/health" in source


def test_launcher_records_startup_exceptions_for_pythonw_users() -> None:
    from lead_shrimp import launcher

    source = __import__("inspect").getsource(launcher.main)

    assert "traceback" in source
    assert "_startup_error_file" in source


def test_app_disables_uvicorn_console_log_config_for_pythonw() -> None:
    from lead_shrimp import app

    source = __import__("inspect").getsource(app.main)

    assert "log_config=None" in source


def test_collection_failure_is_returned_as_json_for_the_frontend(monkeypatch) -> None:
    from lead_shrimp import app as app_module

    monkeypatch.setattr(app_module.comment_leads, "run_monitor", lambda _monitor_id: (_ for _ in ()).throw(RuntimeError("浏览器连接已断开")))
    response = TestClient(app_module.app).post("/api/comment-leads/run", json={"monitor_id": "monitor-test"})

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "评论采集失败：浏览器连接已断开"


def test_frontend_explains_partial_collection_and_non_json_errors() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert "服务端返回了无法识别的数据" in page
    assert "部分完成" in page


def test_frontend_only_auto_opens_activation_when_license_is_required() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert "const required=Boolean(data.license_required ?? data.enforced);" in page
    assert "if(!ok&&required)$('licenseModal').classList.add('show');" in page
    assert "本地模式可直接使用" in page


def test_collection_diagnosis_turns_missing_login_into_a_next_step(monkeypatch) -> None:
    from lead_shrimp import app as app_module

    monkeypatch.setattr(app_module.comment_leads, "list_monitors", lambda: [{"id": "monitor-a", "last_count": 20, "last_error": "未登录抖音，请先点击授权登录", "last_run_at": 123, "discovered_video_count": 5}])
    monkeypatch.setattr(app_module.comment_leads, "login_status", lambda: {"logged_in": False, "has_profile": True, "cookie_count": 0})

    response = TestClient(app_module.app).get("/api/comment-leads/diagnosis?monitor_id=monitor-a")

    assert response.status_code == 200
    assert response.json()["next_action"] == "login"
    assert response.json()["title"] == "需要完成抖音登录"
    assert response.json()["captured"] == 20


def test_comment_login_state_survives_page_refresh(monkeypatch, tmp_path) -> None:
    from lead_shrimp import app as app_module

    profile = tmp_path / "comment_profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    state_path = tmp_path / "comment_login_state.json"
    monkeypatch.setattr(app_module.comment_leads.config, "COMMENT_LEADS_PROFILE_DIR", profile)
    monkeypatch.setattr(app_module.comment_leads.config, "COMMENT_LEADS_LOGIN_STATE_JSON", state_path)
    monkeypatch.setattr(app_module.comment_leads.browser_cookies, "shared_status", lambda: {"has_login": False, "cookie_count": 0, "browser": "msedge"})

    app_module.comment_leads._save_login_state(True, "msedge")
    first = TestClient(app_module.app).get("/api/comment-leads/status").json()
    second = TestClient(app_module.app).get("/api/comment-leads/status").json()

    assert first["logged_in"] is True
    assert second["logged_in"] is True
    assert first["expires_at"] > first.get("cookie_count", 0)


def test_expired_comment_login_state_requires_relogin(monkeypatch, tmp_path) -> None:
    from lead_shrimp import app as app_module

    profile = tmp_path / "comment_profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    state_path = tmp_path / "comment_login_state.json"
    state_path.write_text('{"authenticated": true, "browser": "msedge", "updated_at": 1, "expires_at": 2}', encoding="utf-8")
    monkeypatch.setattr(app_module.comment_leads.config, "COMMENT_LEADS_PROFILE_DIR", profile)
    monkeypatch.setattr(app_module.comment_leads.config, "COMMENT_LEADS_LOGIN_STATE_JSON", state_path)
    monkeypatch.setattr(app_module.comment_leads.browser_cookies, "shared_status", lambda: {"has_login": False, "cookie_count": 0, "browser": "msedge"})

    response = TestClient(app_module.app).get("/api/comment-leads/status")

    assert response.status_code == 200
    assert response.json()["logged_in"] is False


def test_frontend_has_a_fast_diagnosis_area() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert "快速排错" in page
    assert "/api/comment-leads/diagnosis" in page


def test_frontend_uses_a_compact_workbench_and_visible_controls() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert ".app{max-width:none" in page
    assert ".tabs{width:184px" in page
    assert ".tab-btn{border:1px solid #d8e2ef;background:#fff" in page
    assert ".tab-btn.active{background:var(--brand)" in page


def test_completed_setup_status_bar_collapses_to_reduce_top_blank_space() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert ".setup.is-complete{display:none}" in page
    assert "$('setupPanel').classList.toggle('is-complete'" in page


def test_frontend_groups_lead_filters_into_a_compact_workbench_toolbar() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert '<div class="toolbar lead-toolbar">' in page
    assert 'class="date-range"' in page
    assert 'class="selection-tools"' in page
    assert ".lead-toolbar{" in page
    assert ".date-range{" in page
    assert ".selection-tools{" in page


def test_work_picker_defaults_to_recent_non_pinned_videos() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert 'id="workAgeFilter"' in page
    assert 'id="includePinnedWorks"' in page
    assert "const DEFAULT_WORK_WINDOW_DAYS=30;" in page
    assert "v.selected===true?'checked':''" in page


def test_profile_render_waits_for_api_metadata_before_dom_fallback() -> None:
    from pipeline.short_video import _render_profile_events_locked

    source = __import__("inspect").getsource(_render_profile_events_locked)

    assert "tick >= 14" in source


def test_profile_fallback_enriches_video_ids_from_detail_endpoint() -> None:
    from pipeline import short_video

    source = __import__("inspect").getsource(short_video._enrich_profile_videos)

    assert "aweme/detail" in source
    assert "aweme_id" in source


def test_incomplete_profile_cache_does_not_short_circuit_metadata_refresh() -> None:
    from pipeline import short_video

    source = __import__("inspect").getsource(short_video.resolve_profile)

    assert "_profile_video_has_metadata" in source


def test_work_picker_shows_pinned_works_and_marks_them_without_selecting() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert '<option value="0" selected>全部作品</option>' in page
    assert "$('includePinnedWorks').checked=true;" in page
    assert "${v.pinned?'<span class=\"chip warn\">置顶</span>':''}" in page


def test_profile_metadata_retries_captured_post_endpoint() -> None:
    from pipeline.short_video import _render_profile_events_locked

    source = __import__("inspect").getsource(_render_profile_events_locked)

    assert "metadata_retry" in source
    assert "dom_fallback_videos" in source


def test_profile_post_endpoint_retries_are_bounded() -> None:
    from pipeline.short_video import _render_profile_events_locked

    source = __import__("inspect").getsource(_render_profile_events_locked)

    assert "post_api_attempts" in source
    assert "post_api_attempts[api_url]" in source


def test_profile_api_requests_have_a_per_run_budget() -> None:
    from pipeline.short_video import _render_profile_events_locked

    source = __import__("inspect").getsource(_render_profile_events_locked)

    assert "profile_api_request_count" in source
    assert "profile_api_request_count >= 4" in source
    assert "post_api_urls[-2:]" in source


def test_author_comments_are_removed_before_lead_ingestion() -> None:
    from pipeline.comment_leads import filter_author_comments

    rows = [
        {"comment_id": "author", "commenter_sec_uid": "sec-author", "content": "作者评论"},
        {"comment_id": "visitor", "commenter_sec_uid": "sec-visitor", "content": "用户评论"},
    ]

    filtered = filter_author_comments(rows, author_sec_uid="sec-author")

    assert [row["comment_id"] for row in filtered] == ["visitor"]


def test_profile_work_cache_is_used_before_browser_resolution() -> None:
    from pipeline import comment_leads

    source = __import__("inspect").getsource(comment_leads.resolve_profile_works)

    assert "cached_videos" in source
    assert "force" in source
    assert "已使用本地作品缓存" in source


def test_frontend_has_lead_time_filters_and_cached_work_refresh() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert 'id="leadAgeFilter"' in page
    assert 'id="leadSort"' in page
    assert 'data-refresh="' in page
    assert "payload.force=true" in page


def test_selected_lead_export_only_accepts_existing_ids(tmp_path, monkeypatch) -> None:
    from lead_shrimp import app as app_module

    export_dir = tmp_path / "exports"
    store_path = tmp_path / "leads.json"
    monkeypatch.setattr(app_module.comment_leads.config, "COMMENT_LEADS_JSON", store_path)
    monkeypatch.setattr(app_module.comment_leads.config, "COMMENT_LEADS_EXPORT_DIR", export_dir)
    store_path.write_text(
        '{"version": 1, "monitors": [], "leads": ['
        '{"lead_id":"lead-a","comment_id":"comment-a","content":"高价值"},'
        '{"lead_id":"lead-b","comment_id":"comment-b","content":"普通"}], "jobs": []}',
        encoding="utf-8",
    )

    response = TestClient(app_module.app).post(
        "/api/comment-leads/export", json={"lead_ids": ["lead-a"]}
    )

    assert response.status_code == 200
    assert "高价值" in response.content.decode("utf-8-sig")
    assert "普通" not in response.content.decode("utf-8-sig")


def test_frontend_supports_selective_lead_export() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert 'id="selectVisibleLeads"' in page
    assert 'data-lead-id=' in page
    assert "导出选中" in page
    assert "/api/comment-leads/export" in page


def test_comment_collection_default_is_not_limited_to_100() -> None:
    from lead_shrimp.app import frontend_path
    from pipeline import comment_leads

    page = frontend_path().read_text(encoding="utf-8")
    source = __import__("inspect").getsource(comment_leads.capture_video_comments)

    assert '<option value="500" selected>500 条</option>' in page
    assert "max_comments: int = 500" in source
    assert "max_capture_seconds = min(300" in source


def test_work_selection_has_a_comment_count_control() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert 'id="workCommentLimit"' in page
    assert 'option value="500" selected' in page
    assert "$('workCommentLimit').value" in page


def test_cached_works_restore_after_refresh_and_warn_after_one_day() -> None:
    from lead_shrimp.app import frontend_path
    from pipeline import comment_leads

    page = frontend_path().read_text(encoding="utf-8")
    source = __import__("inspect").getsource(comment_leads._profile_monitor_metadata)

    assert "works_cached_at" in source
    assert "function loadCachedWorks" in page
    assert "超过 1 天未刷新作品" in page
    assert "cached_videos" in page


def test_comment_capture_is_headless_until_verification_is_detected() -> None:
    from pipeline import comment_leads

    source = __import__("inspect").getsource(comment_leads.capture_video_comments)

    assert "headed: bool = True" in source
    assert "background: bool = True" in source
    assert "_page_needs_verification" in source
    assert "allow_interactive_fallback" in source


def test_background_capture_does_not_change_login_window_launch() -> None:
    from pipeline import comment_leads

    source = __import__("inspect").getsource(comment_leads._launch_comment_context)

    assert "background" in source
    assert "--start-minimized" in source
    assert "--window-position=-32000,-32000" in source


def test_lead_list_supports_drag_selection_and_date_range_selection() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert 'id="leadStartDate"' in page
    assert 'id="leadEndDate"' in page
    assert 'id="dragSelectLeads"' in page
    assert "selectLeadsByDate" in page
    assert "leadRangeSlider" in page


def test_drag_selection_uses_one_vertical_range_slider_before_checkbox() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert ".lead-range-rail" in page
    assert 'id="leadRangeSlider"' in page
    assert 'id="leadRangeThumb"' in page
    assert 'id="leadRangeRail"' in page
    assert "selectLeadsToIndex" in page
    assert "onpointermove" in page
    assert "style.top" in page
    assert "rows.forEach((row,i)=>setLeadSelection(row.lead_id,i<=end&&!state.rangeExcludedIds.has(String(row.lead_id||''))))" in page
    assert ".lead-range-rail{position:absolute;left:10px;top:42px;bottom:0;width:36px" in page
    assert ".lead-range-slider{position:relative;width:36px" in page
    assert "dragSelectMode:true" in page
    assert "applyRangeToRows" in page
    assert "rowIndexAtY" in page
    assert "getBoundingClientRect" in page
    assert "event.clientY" in page
    assert "setPointerCapture" in page
    assert "requestAnimationFrame" in page
    assert "scrollHeight" in page
    assert "style.height" in page
    assert "rangeExcludedIds" in page
    assert "rangeExcludedIds.add" in page
    assert "!state.rangeExcludedIds.has" in page
    assert "滑动选择已开启" in page


def test_monitor_list_is_a_compact_avatar_grid() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert ".monitor-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr))" in page
    assert "class=\"monitor-identity\"" in page
    assert "class=\"monitor-avatar\"" in page
    assert 'const url=escapeHtml(m.target_url||m.raw_url||'');' not in page
    assert '作品 ${escapeHtml(m.max_videos||5)} 条' not in page


def test_work_resolution_has_visible_progress_before_request_finishes() -> None:
    from lead_shrimp.app import frontend_path

    page = frontend_path().read_text(encoding="utf-8")

    assert 'id="workLoading"' in page
    assert 'id="workLoadingText"' in page
    assert "function setWorkLoading" in page
    assert "switchTab('worksTab');" in page
    assert "$('resolveWorksBtn').disabled=active;" in page
