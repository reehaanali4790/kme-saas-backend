"""
Upstash Redis Client - Caching and Ephemeral Storage Connection
"""
import logging
from typing import Optional
import redis

from config.settings import settings

logger = logging.getLogger("uvicorn")


class RedisClient:
    """Production-ready Upstash Redis Client with connection pooling and safety fallbacks."""

    def __init__(self):
        self.client = None
        self.enabled = False

        if settings.REDIS_URL:
            try:
                # Use connection pooling
                # We decode responses to UTF-8 strings automatically
                self.pool = redis.ConnectionPool.from_url(
                    settings.REDIS_URL,
                    decode_responses=True,
                    socket_connect_timeout=2.0,  # 2-second timeout for quick fallback
                    socket_timeout=2.0
                )
                self.client = redis.Redis(connection_pool=self.pool)
                # Verify connection immediately
                self.client.ping()
                self.enabled = True
                logger.info("✅ Upstash Redis caching connection initialized successfully.")
            except Exception as e:
                logger.critical(
                    f"❌ Upstash Redis connection failed to initialize: {e}. "
                    f"Caching is disabled, falling back to direct database queries."
                )
                self.client = None
                self.enabled = False
        else:
            logger.info("Upstash Redis URL not set. Caching is disabled.")

    def get(self, key: str) -> Optional[str]:
        if not self.enabled or not self.client:
            return None
        try:
            return self.client.get(key)
        except Exception as e:
            logger.warning(f"⚠️ Redis GET error for key '{key}': {e}. Falling back to database.")
            return None

    def set(self, key: str, value: str, ex: Optional[int] = None, nx: bool = False) -> bool:
        if not self.enabled or not self.client:
            return False
        try:
            result = self.client.set(key, value, ex=ex, nx=nx)
            return bool(result)
        except Exception as e:
            logger.warning(f"⚠️ Redis SET error for key '{key}': {e}.")
            return False

    def delete(self, key: str) -> bool:
        if not self.enabled or not self.client:
            return False
        try:
            self.client.delete(key)
            return True
        except Exception as e:
            logger.warning(f"⚠️ Redis DELETE error for key '{key}': {e}.")
            return False

    def delete_pattern(self, pattern: str) -> bool:
        """Deletes keys matching a glob-like pattern. Useful for cache invalidation."""
        if not self.enabled or not self.client:
            return False
        try:
            keys = self.client.keys(pattern)
            if keys:
                # Upstash Redis / Standard Redis expects unpacked arguments
                self.client.delete(*keys)
            return True
        except Exception as e:
            logger.warning(f"⚠️ Redis DELETE PATTERN error for pattern '{pattern}': {e}.")
            return False


# Initialize the global client instance
redis_cache = RedisClient()
