"""Small desktop launcher for the independently packaged LeadShrimp app."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path


def _install_root() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def app_argv(argv: list[str]) -> list[str]:
    """Strip launcher-only switches before handing control to FastAPI's CLI."""
    return [value for value in argv if value != "--no-browser"]


def with_port(argv: list[str], port: int) -> list[str]:
    """Return argv with the selected service port, preserving unrelated switches."""
    result: list[str] = []
    replaced = False
    for value in argv:
        if value == "--port":
            result.append(value)
            replaced = "pending"
            continue
        if replaced == "pending":
            result.append(str(port))
            replaced = True
            continue
        if isinstance(value, str) and value.startswith("--port="):
            result.append(f"--port={port}")
            replaced = True
            continue
        result.append(value)
    if replaced is False:
        result.extend(["--port", str(port)])
    return result


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_port(preferred: int = 8922, *, attempts: int = 20) -> int:
    """Choose a loopback port so another local service cannot block startup."""
    preferred = max(1024, min(int(preferred), 65535))
    for offset in range(max(1, attempts)):
        candidate = preferred + offset
        if candidate <= 65535 and _port_is_free(candidate):
            return candidate
    raise OSError(f"没有可用的本地端口（已检查 {preferred}-{preferred + attempts - 1}）")


def _requested_port(argv: list[str], default: int = 8922) -> int:
    for index, value in enumerate(argv):
        if value == "--port" and index + 1 < len(argv):
            try:
                return int(argv[index + 1])
            except ValueError:
                return default
        if isinstance(value, str) and value.startswith("--port="):
            try:
                return int(value.split("=", 1)[1])
            except ValueError:
                return default
    return default


def service_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _startup_error_file(root: Path, message: str) -> Path:
    data_dir = Path(os.environ.get("LEADSHRIMP_DATA_DIR") or root / "data")
    log_dir = data_dir.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "launcher.log"
    log_path.write_text(message + "\n", encoding="utf-8")
    error_path = log_dir / "startup-error.html"
    error_path.write_text(
        "<!doctype html><meta charset='utf-8'><title>获客虾启动失败</title>"
        f"<h1>获客虾服务未能启动</h1><p>{message}</p>"
        f"<p>详细日志：<code>{log_path}</code></p>",
        encoding="utf-8",
    )
    return error_path


def wait_for_service_and_open(port: int, *, timeout_sec: float = 30, poll_sec: float = 0.25, root: Path | None = None) -> bool:
    """Open the UI only after Uvicorn answers; show a local diagnostic on timeout."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if service_ready(port):
            webbrowser.open(f"http://127.0.0.1:{port}/", new=1)
            return True
        time.sleep(max(0, poll_sec))
    error = _startup_error_file(root or _install_root(), f"服务启动超时，端口：{port}")
    webbrowser.open(error.as_uri(), new=1)
    return False


def main() -> int:
    root = _install_root()
    os.environ.setdefault("LEADSHRIMP_STANDALONE", "1")
    os.environ.setdefault("LEADSHRIMP_DATA_DIR", str(Path(os.environ.get("LOCALAPPDATA") or root) / "LeadShrimp" / "data"))
    os.environ.setdefault("LEADSHRIMP_ASSET_DIR", str(root / "assets"))
    try:
        port = choose_port(_requested_port(sys.argv))
    except OSError as exc:
        error = _startup_error_file(root, str(exc))
        webbrowser.open(error.as_uri(), new=1)
        return 1
    sys.argv[:] = app_argv(with_port(sys.argv, port))
    try:
        from lead_shrimp.app import main as app_main
    except BaseException:
        error = _startup_error_file(root, traceback.format_exc())
        webbrowser.open(error.as_uri(), new=1)
        return 1
    # A browser is the local UI shell.  It is bound to loopback only; no remote
    # page can command this local collector through CORS.
    if "--no-browser" not in sys.argv:
        threading.Thread(target=wait_for_service_and_open, args=(port,), daemon=True).start()
    try:
        return app_main()
    except BaseException:
        error = _startup_error_file(root, traceback.format_exc())
        if "--no-browser" not in sys.argv:
            webbrowser.open(error.as_uri(), new=1)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
