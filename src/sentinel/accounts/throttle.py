"""Fixed-window rate limits in Redis, shared by every web worker.

Keys are hashed, so Redis never holds e-mail addresses or IP addresses.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from redis.asyncio import Redis


@dataclass(frozen=True)
class Limit:
    bucket: str
    limit: int
    window_s: int


SIGNUP_PER_IP = Limit("signup-ip", 10, 3600)
LOGIN_PER_IP = Limit("login-ip", 30, 600)
LOGIN_PER_ACCOUNT = Limit("login-acct", 10, 600)
RECOVERY_PER_IP = Limit("recovery-ip", 10, 3600)
RECOVERY_PER_ACCOUNT = Limit("recovery-acct", 3, 3600)
TOTP_PER_USER = Limit("totp-user", 10, 600)


class Throttle:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def allow(self, limit: Limit, key: str) -> bool:
        """Count one attempt for ``key``; ``False`` once the window's limit is exceeded."""
        hashed = hashlib.sha256(key.encode()).hexdigest()[:32]
        name = f"rl:{limit.bucket}:{hashed}"
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.incr(name)
            pipe.expire(name, limit.window_s, nx=True)
            count, _ = await pipe.execute()
        return int(count) <= limit.limit
