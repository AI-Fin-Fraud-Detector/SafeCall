import asyncio
from typing import Dict, List

from ..db_manager.database import (
    add_message as db_add_message,  # Alias to avoid name conflict
)
from ..db_manager.database import (
    create_conversation as db_create_conversation,  # Alias to avoid name conflict
)
from ..llm_pipeline import SYSTEM_PROMPT, LLMPipeline


async def handle_chat_message(
    user_id: str,
    prompt: str,
    llm_pipeline: LLMPipeline,
    generate_lock: asyncio.Lock,
    conversation: dict | None = None,
) -> Dict[str, str]:
    conversation_id: str
    messages_history: List[Dict[str, str]]

    if conversation:
        conversation_id = conversation["conversation_id"]
        messages_history = conversation["messages"]

    else:
        conversation_id = await db_create_conversation(user_id)
        messages_history = [{"role": "system", "content": SYSTEM_PROMPT}]

    messages_history.append({"role": "user", "content": prompt})
    await db_add_message(conversation_id, "user", prompt)

    async with generate_lock:
        assistant_response = await asyncio.to_thread(
            llm_pipeline.generate, messages_history
        )

    assistant_message_id = await db_add_message(
        conversation_id, "assistant", assistant_response
    )

    return {
        "response": assistant_response,
        "conversation_id": conversation_id,
        "message_id": assistant_message_id,
    }
