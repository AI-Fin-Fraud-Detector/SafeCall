import json
import os
import secrets
import uuid
from typing import Dict, Optional

import asyncpg
import redis.asyncio as redis

# Database connection pool
pool: asyncpg.Pool | None = None
# Redis client instance
redis_client: redis.Redis | None = None


async def connect_to_db():
    global pool, redis_client
    try:
        # --- PostgreSQL Connection ---
        pool = await asyncpg.create_pool(
            host=os.getenv("DB_HOST", "db"),
            port=os.getenv("DB_PORT", "5432"),
            user=os.getenv("DB_USERNAME", "kebbi"),
            password=os.getenv("DB_PASSWORD", "kebbi"),
            database=os.getenv("DB_DATABASE_NAME", "kebbi"),
        )
        print("[INFO] Database connection pool created successfully.")

        # --- Redis Connection ---
        redis_client = redis.from_url("redis://redis:6379/0", decode_responses=True)
        print("[INFO] Redis client created successfully.")
    except Exception as e:
        print(f"[ERROR] Failed to create database connection pool: {e}")
        raise


async def close_db_connection():
    global pool
    if pool:
        await pool.close()
        print("[INFO] Database connection pool closed.")


async def close_redis_connection():
    global redis_client
    if redis_client:
        await redis_client.close()
        print("[INFO] Redis client connection closed.")


async def get_user_latest_conversation(
    user_uuid: str, target_user_uuid: str
) -> Optional[Dict]:
    """Retrieves the latest conversation for a given user."""
    if pool is None:
        raise Exception("Database connection pool is not initialized.")
    async with pool.acquire() as conn:
        conversation_record = await conn.fetchrow(
            """
            SELECT id, title, created_at, updated_at FROM fraud_conversations
            WHERE user_uuid = $1
            AND target_user_uuid = $2
            ORDER BY updated_at DESC
            LIMIT 1;
            """,
            uuid.UUID(user_uuid),
            uuid.UUID(target_user_uuid),
        )

        if not conversation_record:
            print(
                f"[INFO] No conversation found for user {user_uuid} with target {target_user_uuid}"
            )
            return None

        messages_records = await conn.fetch(
            """
            SELECT id, role, content FROM fraud_messages
            WHERE conversation_id = $1
            ORDER BY created_at ASC;
            """,
            conversation_record["id"],
        )

        messages = [
            {"id": str(r["id"]), "role": r["role"], "content": r["content"]}
            for r in messages_records
        ]
        return {
            "conversation_id": str(conversation_record["id"]),
            "title": conversation_record["title"],
            "messages": messages,
            "created_at": conversation_record["created_at"],
            "updated_at": conversation_record["updated_at"],
        }


async def create_conversation(
    user_uuid: str, target_user_uuid: str
) -> str:  # Removed system_prompt parameter
    """Creates a new conversation and adds the system message."""
    if pool is None:
        raise Exception("Database connection pool is not initialized.")
    async with pool.acquire() as conn:
        conversation_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO fraud_conversations (id, user_uuid, target_user_uuid)
            VALUES ($1, $2, $3);
            """,
            conversation_id,
            uuid.UUID(user_uuid),
            uuid.UUID(target_user_uuid),
        )
        print(
            f"[INFO] Created new conversation {conversation_id} for user {user_uuid} with target {target_user_uuid}"
        )
        # await add_message(str(conversation_id), "system", SYSTEM_PROMPT) # Use imported SYSTEM_PROMPT
        return str(conversation_id)


async def add_message(conversation_id: str, role: str, content: str) -> str:
    """Adds a message to an existing conversation."""
    if pool is None:
        raise Exception("Database connection pool is not initialized.")
    async with pool.acquire() as conn:
        message_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO fraud_messages (id, conversation_id, role, content)
            VALUES ($1, $2, $3, $4);
            """,
            message_id,
            uuid.UUID(conversation_id),
            role,
            content,
        )
        # Update conversation's updated_at timestamp
        await conn.execute(
            """
            UPDATE fraud_conversations
            SET updated_at = NOW()
            WHERE id = $1;
            """,
            uuid.UUID(conversation_id),
        )
        return str(message_id)


async def set_call_token(caller_id: str, callee_id: str, expiration_seconds: int = 60):
    """Sets a call token for a user in Redis with an expiration time."""
    if redis_client is None:
        raise Exception("Redis client is not initialized.")

    token = secrets.token_urlsafe(32)
    await redis_client.set(
        f"call_token:{token}",
        json.dumps(
            {
                "caller": caller_id,
                "callee": callee_id,
            }
        ),
        ex=expiration_seconds,
    )
    return token
