from licensing_server.rate_limit import IpRateLimiter, RateLimitPolicy, client_ip_from_request


def test_trusted_proxy_uses_forwarded_client_ip_but_direct_client_cannot_spoof_it():
    assert client_ip_from_request("127.0.0.1", "203.0.113.9, 10.0.0.5", {"127.0.0.1"}) == "203.0.113.9"
    assert client_ip_from_request("198.51.100.4", "203.0.113.9", {"127.0.0.1"}) == "198.51.100.4"


def test_rate_limiter_rejects_a_path_after_its_window_limit():
    limiter = IpRateLimiter(RateLimitPolicy(window_seconds=60, activate_attempts=2, refresh_attempts=5))

    assert limiter.allow(path="/v1/activate", client_ip="203.0.113.9", now=100)[0] is True
    assert limiter.allow(path="/v1/activate", client_ip="203.0.113.9", now=101)[0] is True
    allowed, retry_after = limiter.allow(path="/v1/activate", client_ip="203.0.113.9", now=102)

    assert allowed is False
    assert retry_after == 58
    assert limiter.allow(path="/v1/activate", client_ip="203.0.113.9", now=161)[0] is True
