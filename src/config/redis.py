"""
Optional Redis client. Import `get_redis()` where caching, rate limiting
or pub/sub is needed. Kept lazy so the app still boots if Redis isn't
running in local development.
"""
from functools import lru_cache

import redis

from src.config.env import settings


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        db=settings.REDIS_DB,
        decode_responses=True,
    )
