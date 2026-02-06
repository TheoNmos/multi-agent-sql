import redis.asyncio as redis
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

from app.config import redis_settings

# Global connection pool (reused across the application)
_redis_pool: ConnectionPool | None = None
_redis_client: Redis | None = None


def get_redis_pool() -> ConnectionPool:
    """Get or create the Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = redis.ConnectionPool.from_url(
            str(redis_settings.redis_url),
            max_connections=30,
            decode_responses=True,  # Return strings instead of bytes
            socket_keepalive=True,
            socket_timeout=5,
        )
    return _redis_pool


async def get_redis_client() -> Redis:
    """Get or create the Redis client."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(connection_pool=get_redis_pool())
    return _redis_client


async def get_raw_redis_connection() -> Redis:
    """Get the raw Redis connection (for backwards compatibility)."""
    return await get_redis_client()
