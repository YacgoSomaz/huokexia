"""Scan a release/staging directory for files that must never ship.

Usage:
    python packaging/build/check_release.py path/to/staging

The scanner is intentionally conservative. If it finds runtime data, cookies,
developer tests, prompt drafts, databases, audio/video, or likely secrets, the
build should fail before an installer is produced.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FORBIDDEN_DIR_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "tests",
    "logs",
    "audio",
    "video",
    "exports",
    "avatar_cache",
    "short_video_assets",
    "comment_leads_browser_profile",
    "license_data",
    "_screenshots",
}

FORBIDDEN_FILE_NAMES = {
    "ai_config.json",
    "browser_cookies.json",
    "short_video_cookies.json",
    "rooms.json",
    "pending_anchors.json",
    "anchor_profiles.json",
    "short_video_profiles.json",
    "short_video_parse_cache.json",
    "short_video_jobs.json",
    "short_video_benchmarks.json",
    "comment_leads.json",
    "comment_leads_seen.json",
    "license.json",
    "license_clock.json",
    "account_session.json",
    "HANDOFF.md",
    "CLAUDE_HANDOFF.md",
}

FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".mp3",
    ".mp4",
    ".wav",
    ".flv",
    ".har",
    ".pcap",
    ".pfx",
    ".p12",
    ".key",
    ".log",
    ".pyc",
    ".map",
}

FORBIDDEN_NAME_PATTERNS = [
    re.compile(r"^_scratch_.*", re.I),
    re.compile(r"^_tmp_.*", re.I),
    re.compile(r".*\.bak(_.*)?$", re.I),
    re.compile(r".*handoff.*", re.I),
]

SECRET_PATTERNS = [
    re.compile(rb"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(rb"Bearer\s+[A-Za-z0-9._-]{20,}", re.I),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
    re.compile(rb"sessionid(_ss)?\s*[:=]"),
    re.compile(rb"passport_(csrf_token|auth_status)\s*[=:]\s*[A-Za-z0-9%._-]{8,}"),
]
PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")

TEXT_SUFFIXES = {
    ".py",
    ".js",
    ".html",
    ".css",
    ".json",
    ".txt",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".ps1",
    ".bat",
    ".iss",
}


def _is_forbidden_path(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    lower_parts = {part.lower() for part in rel.parts}
    for dirname in FORBIDDEN_DIR_NAMES:
        if dirname.lower() in lower_parts:
            return f"forbidden directory: {dirname}"
    name = path.name
    if name in FORBIDDEN_FILE_NAMES:
        return f"forbidden file: {name}"
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return f"forbidden suffix: {path.suffix}"
    for pattern in FORBIDDEN_NAME_PATTERNS:
        if pattern.fullmatch(name):
            return f"forbidden filename pattern: {pattern.pattern}"
    return ""


def _skip_vendor_content_scan(rel: Path) -> bool:
    parts = tuple(part.lower() for part in rel.parts)
    if parts and parts[0] == "_internal":
        return True
    return parts[:3] in {
        ("app", "pipeline_data", "static"),
        ("app", "pipeline", "static"),
    }


def scan_release(root: Path, *, commercial: bool = False) -> list[str]:
    root = root.resolve()
    if not root.exists():
        return [f"release path does not exist: {root}"]
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        reason = _is_forbidden_path(path, root)
        if reason:
            findings.append(f"{rel} -> {reason}")
            continue
        if commercial and len(rel.parts) >= 2:
            app_subdir = (rel.parts[0].lower(), rel.parts[1].lower())
            if app_subdir in {("app", "pipeline"), ("app", "vendor"), ("app", "third_party")}:
                findings.append(f"{rel} -> source/vendor directories are not allowed in a commercial release")
                continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            data = path.read_bytes()[:2_000_000]
        except OSError as exc:
            findings.append(f"{path.relative_to(root)} -> unreadable: {exc}")
            continue
        if PRIVATE_KEY_PATTERN.search(data):
            findings.append(f"{rel} -> private-key material")
            continue
        if _skip_vendor_content_scan(rel):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                findings.append(f"{rel} -> secret-like content: {pattern.pattern.decode('latin1')}")
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan release directory for sensitive or developer-only files.")
    parser.add_argument("path", type=Path, help="Release/staging directory to scan")
    parser.add_argument("--commercial", action="store_true", help="Reject business Python source under app/pipeline")
    args = parser.parse_args(argv)
    findings = scan_release(args.path, commercial=args.commercial)
    if findings:
        print("Release scan failed:")
        for item in findings:
            print(f"  - {item}")
        return 1
    print(f"Release scan passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
