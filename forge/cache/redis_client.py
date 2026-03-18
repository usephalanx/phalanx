"""
Redis client — async connection pool.
All Redis access goes through this module.
Never use Redis for authoritative state — Postgres is truth.
"""
import redis.asyncio as aioredis
from typing import AsyncIterator
from contextlib import asynccontextmanager
from forge.config.settings import get_settings

settings = get_settings()

_redis_pool: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis:
    """Get Redis connection from pool. Call once per application lifecycle."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis_pool


async def close_redis():
    global _redis_pool
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_pool = None


@asynccontextmanager
async def redis_lock(key: str, timeout: int = 30) -> AsyncIterator[bool]:
    """
    Distributed lock using Redis SET NX EX.
    Usage:
        async with redis_lock("lock:run:abc123") as acquired:
            if not acquired:
                return  # someone else has the lock
            # do work
    """
    r = await get_redis()
    lock_key = f"forge:lock:{key}"
    acquired = await r.set(lock_key, "1", nx=True, ex=timeout)
    try:
        yield bool(acquired)
    finally:
        if acquired:
            await r.delete(lock_key)
