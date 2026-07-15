"""Small in-memory rate limiter for the public licensing endpoints.

The service is deliberately single-process at this scale.  If it later moves
behind multiple workers, replace this module with a shared Redis-backed store.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitPolicy:
    window_seconds: int = 60
    activate_attempts: int = 8
    refresh_attempts: int = 60

    def limit_for(self, path: str) -> int | None:
        if path == "/v1/activate":
            return self.activate_attempts
        if path == "/v1/refresh":
            return self.refresh_attempts
        return None


def client_ip_from_request(remote_ip: str, forwarded_for: str, trusted_proxies: set[str]) -> str:
    """Use X-Forwarded-For only when the TCP peer is our own reverse proxy."""
    remote = (remote_ip or "").strip()
    if remote in trusted_proxies and forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first[:128]
    return remote[:128] or "unknown"


class IpRateLimiter:
    def __init__(self, policy: RateLimitPolicy | None = None) -> None:
        self.policy = policy or RateLimitPolicy()
        if self.policy.window_seconds < 1 or self.policy.activate_attempts < 1 or self.policy.refresh_attempts < 1:
            raise ValueError("限流配置必须为正数")
        self._buckets: dict[tuple[str, str], deque[int]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, *, path: str, client_ip: str, now: int | None = None) -> tuple[bool, int]:
        limit = self.policy.limit_for(path)
        if limit is None:
            return True, 0
        current = int(now if now is not None else time.time())
        key = (path, client_ip)
        with self._lock:
            bucket = self._buckets[key]
            cutoff = current - self.policy.window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return False, max(1, bucket[0] + self.policy.window_seconds - current)
            bucket.append(current)
            return True, 0
