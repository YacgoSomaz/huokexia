"""Small, testable browser-origin policy for the local desktop console."""

from __future__ import annotations

import re


# The packaged WebView2 shell and a normal local browser both use these origins.
# Do not broaden this to arbitrary websites: they must not drive localhost APIs.
LOCAL_UI_ORIGIN_REGEX = r"^https?://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$"
_LOCAL_UI_ORIGIN = re.compile(LOCAL_UI_ORIGIN_REGEX, re.IGNORECASE)


def is_local_ui_origin(origin: str) -> bool:
    """Return whether an Origin header belongs to a loopback UI page."""
    return bool(_LOCAL_UI_ORIGIN.fullmatch((origin or "").strip()))
