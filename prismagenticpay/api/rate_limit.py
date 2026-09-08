from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import HTTPException, Request


class SlidingWindowLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> None:
        now = time.time()
        window = now - 60
        with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] < window:
                bucket.popleft()
            if len(bucket) >= self.per_minute:
                raise HTTPException(status_code=429, detail="rate limit exceeded")
            bucket.append(now)


async def limit_request(request: Request) -> None:
    limiter: SlidingWindowLimiter = request.app.state.limiter
    identity = request.headers.get("X-API-Key") or (request.client.host if request.client else "unknown")
    limiter.check(identity)
