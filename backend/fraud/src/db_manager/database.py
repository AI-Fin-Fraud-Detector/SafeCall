import json
import os
import re
import secrets
import uuid
from typing import Dict, Optional

import asyncpg
import redis.asyncio as redis

# Database connection pool
pool: asyncpg.Pool | None = None
# Redis client instance
redis_client: redis.Redis | None = None
PHONE_DIGIT_RE = re.compile(r"\D+")


async def _init_connection(conn: asyncpg.Connection):
    """
    Configure the PostgreSQL connection to encode and decode JSONB values as JSON.
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def connect_to_db():
    global pool, redis_client
    try:
        # --- PostgreSQL Connection ---
        pool = await asyncpg.create_pool(
            host=os.getenv("DB_HOST", "db"),
            port=os.getenv("DB_PORT", "5432"),
            user=os.getenv("DB_USERNAME", "safecall"),
            password=os.getenv("DB_PASSWORD", "safecall"),
            database=os.getenv("DB_DATABASE_NAME", "safecall"),
            init=_init_connection,
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
    user_uuid: str,
) -> Optional[Dict]:
    """Retrieves the latest conversation for a given user."""
    if pool is None:
        raise Exception("Database connection pool is not initialized.")
    async with pool.acquire() as conn:
        conversation_record = await conn.fetchrow(
            """
            SELECT id, title, created_at, updated_at FROM conversations
            WHERE user_uuid = $1 AND type = 'phone'
            ORDER BY updated_at DESC
            LIMIT 1;
            """,
            uuid.UUID(user_uuid),
        )

        if not conversation_record:
            print(f"[INFO] No conversation found for user {user_uuid}")
            return None

        messages_records = await conn.fetch(
            """
            SELECT id, role, content FROM messages
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
    user_uuid: str,
    caller_phone_number: str,
    caller_name: str | None,
    caller_type: str,
) -> str:
    """
    Create a phone conversation for a user with caller metadata.
    
    Parameters:
        user_uuid (str): UUID of the user associated with the conversation.
        caller_phone_number (str): Phone number of the caller.
        caller_name (str | None): Optional name of the caller.
        caller_type (str): Type of caller.
    
    Returns:
        str: The identifier of the newly created conversation.
    """
    if pool is None:
        raise Exception("Database connection pool is not initialized.")
    async with pool.acquire() as conn:
        conversation_id = uuid.uuid4()
        await conn.execute(
            """
            INSERT INTO conversations (id, user_uuid, type, metadata)
            VALUES ($1, $2, 'phone', $3::jsonb);
            """,
            conversation_id,
            uuid.UUID(user_uuid),
            {
                "caller_phone_number": caller_phone_number,
                "caller_name": caller_name,
                "caller_type": caller_type,
            },
        )
        print(f"[INFO] Created new conversation {conversation_id} for user {user_uuid}")
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
            INSERT INTO messages (id, conversation_id, role, content)
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
            UPDATE conversations
            SET updated_at = NOW()
            WHERE id = $1;
            """,
            uuid.UUID(conversation_id),
        )
        return str(message_id)


async def is_fraud_detection_enabled(user_uuid: str) -> bool:
    """Checks if fraud detection is enabled for a given user."""
    if pool is None:
        raise Exception("Database connection pool is not initialized.")
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            """
            SELECT scam_detection FROM users
            WHERE uuid = $1;
            """,
            uuid.UUID(user_uuid),
        )
        return result


async def set_call_token(
    caller_id: str,
    callee_id: str,
    conversation_id: str,
    caller_name: str | None = None,
    caller_type: str | None = None,
    expiration_seconds: int = 60,
):
    """
    Create a temporary token containing call participant and conversation details.
    
    Parameters:
        caller_name: Optional name associated with the caller.
        caller_type: Optional type associated with the caller.
        expiration_seconds: Number of seconds before the token expires.
    
    Returns:
        The generated call token.
    """
    if redis_client is None:
        raise Exception("Redis client is not initialized.")

    token = secrets.token_urlsafe(32)
    await redis_client.set(
        f"call_token:{token}",
        json.dumps(
            {
                "caller": caller_id,
                "callee": callee_id,
                "conversation_id": conversation_id,
                "caller_name": caller_name,
                "caller_type": caller_type,
            }
        ),
        ex=expiration_seconds,
    )
    return token


async def consume_call_token(token: str) -> Optional[Dict]:
    """Retrieve and consume a call token.
    
    Parameters:
    	token (str): The token to retrieve.
    
    Returns:
    	Optional[Dict]: The token payload, or `None` if the token does not exist.
    """
    if redis_client is None:
        raise Exception("Redis client is not initialized.")

    raw = await redis_client.getdel(f"call_token:{token}")
    if not raw:
        return None
    return json.loads(raw)


async def get_call_token(token: str) -> Optional[Dict]:
    """Gets a call token payload without consuming it."""
    if redis_client is None:
        raise Exception("Redis client is not initialized.")

    raw = await redis_client.get(f"call_token:{token}")
    if not raw:
        return None
    return json.loads(raw)


async def get_contact_by_phone(user_uuid: str, phone_number: str) -> Optional[Dict]:
    """
    Find a user's contact by phone number.
    
    Parameters:
    	user_uuid (str): The user's UUID.
    	phone_number (str): The phone number to normalize and search for.
    
    Returns:
    	Optional[Dict]: The matching contact details, or `None` if the phone number is empty, no contact matches, or the contacts table is unavailable.
    """
    if pool is None:
        raise Exception("Database connection pool is not initialized.")

    normalized_phone_number = normalize_phone_number(phone_number)
    if not normalized_phone_number:
        return None

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT id, name, phone_number, normalized_phone_number
                FROM contacts
                WHERE user_uuid = $1 AND normalized_phone_number = $2
                LIMIT 1
                """,
                uuid.UUID(user_uuid),
                normalized_phone_number,
            )
        except asyncpg.UndefinedTableError:
            return None
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "name": row["name"],
            "phone_number": row["phone_number"],
            "normalized_phone_number": row["normalized_phone_number"],
        }


def normalize_phone_number(phone_number: str) -> str:
    """Normalize a phone number to its digit-only representation.
    
    Converts numbers beginning with `886` and containing 12 digits to the corresponding local format beginning with `0`.
    
    Parameters:
        phone_number (str): The phone number to normalize.
    
    Returns:
        str: The normalized phone number.
    """
    normalized = PHONE_DIGIT_RE.sub("", phone_number or "")
    if normalized.startswith("886") and len(normalized) == 12:
        return f"0{normalized[3:]}"
    return normalized

