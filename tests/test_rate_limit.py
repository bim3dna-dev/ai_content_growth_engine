from growth_engine.rate_limit import RateLimiter


def test_platform_limits_are_independent_and_reset() -> None:
    limiter = RateLimiter({"youtube": 2, "tiktok": 1})
    assert limiter.allow("youtube", now=0)
    assert limiter.allow("youtube", now=1)
    assert not limiter.allow("youtube", now=2)
    assert limiter.allow("tiktok", now=2)
    assert not limiter.allow("tiktok", now=3)
    assert limiter.allow("youtube", now=61)
