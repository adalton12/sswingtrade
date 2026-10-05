"""
Redis cache service for session data and caching.
"""

import json
from typing import Any, Optional

import redis.asyncio as aioredis
from app.config import settings
from app.services.logger import logger


class CacheService:
    """Redis cache operations."""

    def __init__(self):
        self.redis: Optional[aioredis.Redis] = None

    async def connect(self):
        """Connect to Redis."""
        try:
            self.redis = await aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
            )
            await self.redis.ping()
            logger.info("✅ Connected to Redis")
        except Exception as e:
            logger.error(f"❌ Redis connection failed: {e}")
            raise

    async def disconnect(self):
        """Disconnect from Redis."""
        if self.redis:
            await self.redis.close()
            logger.info("Redis disconnected")

    async def get(self, key: str) -> Optional[Any]:
        """Get value from cache."""
        try:
            value = await self.redis.get(key)

            if value:
                # Try to deserialize JSON
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value

            return None

        except Exception as e:
            logger.warning(f"Cache GET error for key {key}: {e}")
            return None

    async def set(self, key: str, value: Any, ttl: Optional[int] = None):
        """
        Set value in cache.

        Args:
            key: Cache key
            value: Value to cache (will be JSON serialized if dict/list)
            ttl: Time to live in seconds (default from settings)
        """
        try:
            ttl = ttl or settings.REDIS_CACHE_TTL

            # Serialize value
            if isinstance(value, (dict, list)):
                value = json.dumps(value)

            await self.redis.setex(key, ttl, value)

        except Exception as e:
            logger.warning(f"Cache SET error for key {key}: {e}")

    async def delete(self, key: str):
        """Delete key from cache."""
        try:
            await self.redis.delete(key)
        except Exception as e:
            logger.warning(f"Cache DELETE error for key {key}: {e}")

    async def exists(self, key: str) -> bool:
        """Check if key exists in cache."""
        try:
            return await self.redis.exists(key) > 0
        except Exception as e:
            logger.warning(f"Cache EXISTS error for key {key}: {e}")
            return False

    async def clear_pattern(self, pattern: str):
        """Delete all keys matching pattern."""
        try:
            keys = await self.redis.keys(pattern)
            if keys:
                await self.redis.delete(*keys)
                logger.info(f"Cleared {len(keys)} cache keys matching {pattern}")
        except Exception as e:
            logger.warning(f"Cache CLEAR error for pattern {pattern}: {e}")

    async def incr(self, key: str, amount: int = 1) -> int:
        """Increment counter."""
        try:
            return await self.redis.incrby(key, amount)
        except Exception as e:
            logger.warning(f"Cache INCR error for key {key}: {e}")
            return 0

    async def decr(self, key: str, amount: int = 1) -> int:
        """Decrement counter."""
        try:
            return await self.redis.decrby(key, amount)
        except Exception as e:
            logger.warning(f"Cache DECR error for key {key}: {e}")
            return 0


# Global cache instance
cache = CacheService()


# ============================================================================
# Cache helpers
# ============================================================================

async def check_redis_health() -> dict:
    """Check Redis connectivity."""
    try:
        if cache.redis:
            await cache.redis.ping()
            return {
                "status": "healthy",
                "cache": "connected",
                "message": "Redis connection successful"
            }
        else:
            return {
                "status": "unhealthy",
                "cache": "not_initialized",
                "message": "Redis not initialized"
            }
    except Exception as e:
        return {
            "status": "unhealthy",
            "cache": "disconnected",
            "message": f"Redis connection failed: {str(e)}"
        }


async def get_cached_account(account_id: int) -> Optional[dict]:
    """Get cached account data."""
    return await cache.get(f"account:{account_id}")


async def set_cached_account(account_id: int, account_data: dict):
    """Cache account data."""
    await cache.set(f"account:{account_id}", account_data, ttl=3600)


async def invalidate_account_cache(account_id: int):
    """Invalidate account cache."""
    await cache.delete(f"account:{account_id}")
    await cache.clear_pattern(f"account:{account_id}:*")


if __name__ == "__main__":
    import asyncio

    async def test():
        await cache.connect()

        # Test operations
        await cache.set("test_key", {"message": "Hello"})
        value = await cache.get("test_key")
        print(f"Cached value: {value}")

        await cache.disconnect()

    asyncio.run(test())
