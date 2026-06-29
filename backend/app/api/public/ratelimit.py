"""Fixed-window rate limiter for the public funnel endpoints (S2.2).

# ponytail: per-process fixed-window counter keyed by (slug, client IP). Adequate for
# the single uvicorn process on the v1 VPS. Swap for slowapi + Redis if we ever run
# multiple workers (state would otherwise be per-process and undercount across them).
"""

import threading
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from app.config import settings

_lock = threading.Lock()
# key -> (window_start_monotonic, count)
_hits: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))

# Internet clients can rotate the {slug}/IP key to grow this map; cap it. The limiter
# runs before the product lookup, so unknown slugs land here too. When the map crosses
# the cap we drop fully-expired windows, then hard-clear if still over (a fixed window
# resets anyway — worst case a few clients get one extra window of budget).
_MAX_KEYS = 50_000


def _prune_locked(now: float, window: float) -> None:
    if len(_hits) <= _MAX_KEYS:
        return
    for key in [k for k, (start, _) in _hits.items() if now - start >= window]:
        del _hits[key]
    if len(_hits) > _MAX_KEYS:
        _hits.clear()


def _client_ip(request: Request) -> str:
    # Behind nginx the real client is in X-Forwarded-For (first hop); fall back to the socket.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def reset() -> None:
    """Clear all counters — used by tests to isolate windows."""
    with _lock:
        _hits.clear()


def enforce_rate_limit(request: Request) -> None:
    """FastAPI dependency: 429 once a (slug, IP) exceeds the window budget."""
    slug = request.path_params.get("slug", "")
    key = f"{slug}:{_client_ip(request)}"
    now = time.monotonic()
    window = settings.rate_limit_window_seconds
    limit = settings.rate_limit_requests

    with _lock:
        _prune_locked(now, window)
        start, count = _hits[key]
        if now - start >= window:
            start, count = now, 0
        count += 1
        _hits[key] = (start, count)
        over = count > limit

    if over:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={"Retry-After": str(window)},
        )
