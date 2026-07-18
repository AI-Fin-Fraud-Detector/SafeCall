import re
import uuid
from datetime import datetime
from typing import Optional

import asyncpg
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from .db_manager import database

E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")

router = APIRouter(prefix="/api/fraud", tags=["contacts"])


# --- Pydantic Models ---

class ContactBase(BaseModel):
    name: str = Field(max_length=100)
    phone_number: str = Field(max_length=32)

class ContactCreate(ContactBase):
    pass


class ContactUpdate(ContactBase):
    pass


class Contact(ContactBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)


# --- Endpoints ---

@router.post("/contacts", response_model=Contact, status_code=201)
async def create_contact(
    body: ContactCreate,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    contact_name = body.name.strip()
    if not contact_name:
        raise HTTPException(status_code=400, detail="name cannot be empty.")

    phone_number = body.phone_number.strip()
    if not E164_RE.match(phone_number):
        raise HTTPException(
            status_code=400,
            detail="phone_number must be in E.164 format (e.g. +886912345678).",
        )

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        row = await database.pool.fetchrow(
            """
            INSERT INTO contacts (user_uuid, name, phone_number)
            VALUES ($1, $2, $3)
            RETURNING id, name, phone_number, created_at, updated_at
            """,
            uuid.UUID(x_user_id),
            contact_name,
            phone_number,
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="This contact phone number already exists for the current user.",
        )

    return Contact(**row)


@router.get("/contacts", response_model=list[Contact])
async def list_contacts(
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    rows = await database.pool.fetch(
        """
        SELECT id, name, phone_number, created_at, updated_at
        FROM contacts
        WHERE user_uuid = $1
        ORDER BY created_at DESC
        """,
        uuid.UUID(x_user_id),
    )
    return [Contact(**row) for row in rows]


@router.put("/contacts/{contact_id}", response_model=Contact)
async def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    contact_name = body.name.strip()
    if not contact_name:
        raise HTTPException(status_code=400, detail="name cannot be empty.")

    phone_number = body.phone_number.strip()
    if not E164_RE.match(phone_number):
        raise HTTPException(
            status_code=400,
            detail="phone_number must be in E.164 format (e.g. +886912345678).",
        )

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    try:
        row = await database.pool.fetchrow(
            """
            UPDATE contacts
            SET name = $1,
                phone_number = $2
            WHERE id = $3 AND user_uuid = $4
            RETURNING id, name, phone_number, created_at, updated_at
            """,
            contact_name,
            phone_number,
            contact_id,
            uuid.UUID(x_user_id),
        )
    except asyncpg.exceptions.UniqueViolationError:
        raise HTTPException(
            status_code=409,
            detail="This contact phone number already exists for the current user.",
        )

    if row is None:
        raise HTTPException(status_code=404, detail="Contact not found.")
    return Contact(**row)


@router.delete("/contacts/{contact_id}")
async def delete_contact(
    contact_id: uuid.UUID,
    x_user_id: str | None = Header(None, alias="X-User-Id"),
):
    if not x_user_id:
        raise HTTPException(status_code=400, detail="Missing X-User-Id header")

    if not database.pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    deleted_count = await database.pool.execute(
        "DELETE FROM contacts WHERE id = $1 AND user_uuid = $2",
        contact_id,
        uuid.UUID(x_user_id),
    )
    if deleted_count == "DELETE 0":
        raise HTTPException(status_code=404, detail="Contact not found.")
    return {"message": "Contact deleted successfully."}
