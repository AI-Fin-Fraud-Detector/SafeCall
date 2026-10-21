import os

import asyncpg
import redis.asyncio as aioredis

pool: asyncpg.Pool | None = None
redis_client: aioredis.Redis | None = None


async def connect() -> None:
    global pool, redis_client
    pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "db"),
        port=os.getenv("DB_PORT", "5432"),
        user=os.getenv("DB_USERNAME", "safecall"),
        password=os.getenv("DB_PASSWORD", "safecall"),
        database=os.getenv("DB_DATABASE_NAME", "safecall"),
    )
    redis_client = aioredis.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/0"), decode_responses=True
    )
    await redis_client.ping()
    print("[INFO] Database pool and Redis client created.", flush=True)


async def close() -> None:
    if pool:
        await pool.close()
    if redis_client:
        await redis_client.aclose()


async def get_user(user_uuid: str) -> dict | None:
    """Return the user's display name and every per-channel contact id."""
    assert pool is not None
    row = await pool.fetchrow(
        "SELECT name, line_id FROM users WHERE uuid = $1",
        user_uuid,
    )
    return dict(row) if row else None


async def set_line_id(user_uuid: str, line_id: str) -> bool:
    assert pool is not None
    result = await pool.execute(
        "UPDATE users SET line_id = $1, updated_at = NOW() WHERE uuid = $2",
        line_id,
        user_uuid,
    )
    return result != "UPDATE 0"


async def clear_line_id(user_uuid: str) -> None:
    assert pool is not None
    await pool.execute(
        "UPDATE users SET line_id = NULL, updated_at = NOW() WHERE uuid = $1",
        user_uuid,
    )
