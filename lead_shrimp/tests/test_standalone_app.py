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
