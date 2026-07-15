"""Small desktop launcher for the independently packaged LeadShrimp app."""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path


def _install_root() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def app_argv(argv: list[str]) -> list[str]:
    """Strip launcher-only switches before handing control to FastAPI's CLI."""
    return [value for value in argv if value != "--no-browser"]


def main() -> int:
    root = _install_root()
    os.environ.setdefault("LEADSHRIMP_STANDALONE", "1")
    os.environ.setdefault("LEADSHRIMP_DATA_DIR", str(Path(os.environ.get("LOCALAPPDATA") or root) / "LeadShrimp" / "data"))
    os.environ.setdefault("LEADSHRIMP_ASSET_DIR", str(root / "assets"))
    # A browser is the local UI shell.  It is bound to loopback only; no remote
    # page can command this local collector through CORS.
    if "--no-browser" not in sys.argv:
        threading.Timer(1.1, lambda: webbrowser.open("http://127.0.0.1:8922/", new=1)).start()
    sys.argv[:] = app_argv(sys.argv)
    from lead_shrimp.app import main as app_main

    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
