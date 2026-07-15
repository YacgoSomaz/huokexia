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


def test_public_build_is_unlocked_and_still_compiles_a_standalone_app() -> None:
    script = (ROOT / "build" / "build_public_release.ps1").read_text(encoding="utf-8")

    assert "LICENSE_ENFORCE = False" in script
    assert "--mode=standalone" in script
    assert "check_release.py" in script
    assert "--commercial" not in script
    assert "LICENSE_ADMIN_TOKEN" not in script


def test_public_installer_runs_the_bundled_python_runtime() -> None:
    installer = (ROOT / "build" / "lead_shrimp_public.iss").read_text(encoding="utf-8")

    assert "python\\pythonw.exe" in installer
    assert "-m lead_shrimp.launcher" in installer
    assert "StagingDir" in installer
