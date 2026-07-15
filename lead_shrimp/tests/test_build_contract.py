from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_commercial_build_requires_public_key_and_compiles_sources() -> None:
    script = (ROOT / "build" / "build_commercial_release.ps1").read_text(encoding="utf-8")

    assert "LicensePublicKey" in script
    assert "LICENSE_ENFORCE = True" in script
    assert 'LICENSE_PRODUCT_CODE = "lead_shrimp"' in script
    assert "--mode=standalone" in script
    assert "--include-package=pipeline" in script
    assert "check_release.py" in script
    assert "LICENSE_SIGNING_PRIVATE_KEY" not in script
    assert "LICENSE_ADMIN_TOKEN" not in script


def test_installer_contract_uses_its_own_identity_and_kills_only_its_process() -> None:
    installer = (ROOT / "build" / "lead_shrimp.iss").read_text(encoding="utf-8")

    assert "AppName=获客虾" in installer
    assert "LeadShrimpLauncher.exe" in installer
    assert "CloseApplications=force" in installer
    assert "LeadShrimpLauncher.exe" in installer
    assert "{localappdata}\\LeadShrimp\\data" in installer


def test_start_bat_uses_the_verified_launcher_and_keeps_a_failure_log() -> None:
    launcher = (ROOT / "start_lead_shrimp.bat").read_text(encoding="utf-8")

    assert "lead_shrimp.launcher" in launcher
    assert "lead_shrimp.app" not in launcher
    assert "LeadShrimp-launch.log" in launcher
