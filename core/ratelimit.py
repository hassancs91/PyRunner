"""
Shared fixed-window rate limiting.

One helper for every rate-limit site so the pattern can't drift. The previous
per-site pattern — ``cache.set(key, count + 1, WINDOW)`` — had two bugs:

- ``set()`` re-arms the TTL on every hit, so steady traffic NEVER expires the
  window: a legitimate poller below the limit accumulates across windows and
  eventually starves itself with 429s until a fully quiet window passes.
- The read-modify-write races across gunicorn workers (undercounts bursts).

The window is identified by a time bucket IN THE KEY, not by the cache TTL —
this matters because Django's BaseCache.incr (used by the DatabaseCache
default) is itself a get()+set() that re-arms the TTL, so any TTL-based fixed
window silently turns sliding on that backend. With bucketed keys, a new
window is a new key and nothing a backend does to the old key's TTL can
stretch the budget. The TTL on the bucket key is garbage collection only.
"""

import time

from django.core.cache import cache


def rate_limit_exceeded(key: str, limit: int, window_seconds: int) -> bool:
    """Count one hit against ``key``; True when the fixed window is over budget.

    Allows exactly ``limit`` hits per window (windows are aligned to epoch
    multiples of ``window_seconds``); further hits return True until the next
    window starts. Rejected hits cannot extend the window.

    Fail-open by design: these limits are abuse brakes in front of endpoints
    that carry their own auth (tokens, signatures) — a misbehaving cache must
    not take webhooks down with it.
    """
    bucket = int(time.time() // window_seconds)
    bucket_key = f"{key}:{bucket}"
    cache.add(bucket_key, 0, window_seconds + 60)  # TTL = cleanup only
    try:
        count = cache.incr(bucket_key)
    except ValueError:
        # The key vanished between add() and incr() (cull race). Count this
        # hit on a fresh counter rather than dropping the brake entirely.
        cache.set(bucket_key, 1, window_seconds + 60)
        count = 1
    return count > limit


def rate_limit_over(key: str, limit: int, window_seconds: int) -> bool:
    """Peek: is ``key`` already over budget this window, WITHOUT counting a hit?

    Gates expensive work behind a budget that only specific events consume —
    the plugin API checks this before its token DB lookup so an IP that burned
    its auth-fail budget (counted via ``rate_limit_exceeded`` on each failure)
    stops costing a query per guess. Same fail-open stance as the counter.
    """
    bucket = int(time.time() // window_seconds)
    count = cache.get(f"{key}:{bucket}") or 0
    return count > limit


def seconds_to_window_reset(window_seconds: int) -> int:
    """Seconds until the current fixed window rolls over (for ``Retry-After``)."""
    return max(1, window_seconds - int(time.time()) % window_seconds)


def client_ip(request) -> str:
    """Best-effort client IP for per-IP rate-limit keys.

    Default: ``REMOTE_ADDR`` (the direct peer). Behind a trusted reverse proxy the
    direct peer is the proxy, so every caller shares one IP and per-IP limits become
    instance-global. Set ``RATELIMIT_TRUSTED_PROXY_DEPTH=N`` (N = trusted proxy hops
    in front of PyRunner) to take the client address from the RIGHT of
    ``X-Forwarded-For`` — the entry your outermost trusted proxy appended, which a
    caller cannot forge. Leftmost XFF entries ARE client-controlled and must never
    key a security control, so they are never read here.
    """
    from django.conf import settings

    depth = getattr(settings, "RATELIMIT_TRUSTED_PROXY_DEPTH", 0)
    if depth > 0:
        parts = [
            p.strip()
            for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")
            if p.strip()
        ]
        if len(parts) >= depth:
            return parts[-depth]
    return request.META.get("REMOTE_ADDR", "") or "unknown"
