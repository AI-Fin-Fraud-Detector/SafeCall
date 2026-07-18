import logging
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# --- Configuration ---
DB_DATABASE_NAME = os.getenv("DB_DATABASE_NAME")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")

DATABASE_URL = (
    f"postgresql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_DATABASE_NAME}"
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

# --- Device pairing (QR login) ---
# TTL for the pending phase — how long the QR code stays valid.
DEVICE_PAIRING_TTL_SECONDS = int(os.getenv("DEVICE_PAIRING_TTL_SECONDS", "60"))
DEVICE_POLL_INTERVAL_SECONDS = int(os.getenv("DEVICE_POLL_INTERVAL_SECONDS", "3"))

# --- Password Hashing & Token URL ---
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# --- Database Pool ---
pool: Optional[asyncpg.Pool] = None
redis_client: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Initialize application database and Redis resources, then clean them up during shutdown.
    
    The database schema is prepared before the application starts serving requests. Raises
    an exception if either resource cannot be initialized.
    """
    global pool, redis_client
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        print("Database pool created successfully.")
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await redis_client.ping()
        print("Redis connection established.")
        yield
    finally:
        if pool:
            await pool.close()
            print("Database pool closed.")
        if redis_client:
            await redis_client.aclose()


async def get_db_connection():
    if pool is None:
        raise HTTPException(
            status_code=503, detail="Database connection is not available."
        )
    async with pool.acquire() as connection:
        yield connection


async def get_redis() -> aioredis.Redis:
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Redis is not available.")
    return redis_client


# --- Pydantic Models ---
class UserBase(BaseModel):
    email: str
    phone_number: str
    name: str


class UserCreate(UserBase):
    password: str


class User(UserBase):
    uuid: uuid.UUID
    model_config = ConfigDict(from_attributes=True)


class UserInDB(User):
    hashed_password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# --- Device pairing (QR login) models ---
class DevicePairResponse(BaseModel):
    device_code: str
    pairing_code: str
    expires_in: int
    interval: int


class DeviceApproveRequest(BaseModel):
    pairing_code: str


class DeviceTokenRequest(BaseModel):
    device_code: str


# --- Main Application Instance ---
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Health Check Endpoint ---
@app.get("/health")
async def health_check():
    return {"status": "healthy"}


# --- Utility Functions ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    """
    Hash a plaintext password for secure storage.
    
    Parameters:
    	password (str): The plaintext password to hash.
    
    Returns:
    	str: The resulting password hash.
    """
    return pwd_context.hash(password)


async def create_token(
    conn: asyncpg.Connection,
    user_uuid: uuid.UUID,
    user_agent: Optional[str],
    ip_address: Optional[str],
) -> str:
    """
    Create and persist an authentication token for a user.
    
    Parameters:
    	conn (asyncpg.Connection): Database connection used to store the token.
    	user_uuid (uuid.UUID): UUID of the user associated with the token.
    	user_agent (Optional[str]): Client user-agent value.
    	ip_address (Optional[str]): Client IP address.
    
    Returns:
    	str: The generated authentication token.
    """
    token = secrets.token_urlsafe(32)
    await conn.execute(
        "INSERT INTO tokens (user_uuid, token, user_agent, ip_address) VALUES ($1, $2, $3, $4)",
        user_uuid,
        token,
        user_agent,
        ip_address,
    )
    return token


async def get_user_by_email_from_db(
    conn: asyncpg.Connection, email: str
) -> Optional[UserInDB]:
    row = await conn.fetchrow(
        "SELECT uuid, email, phone_number, name, hashed_password FROM users WHERE email = $1",
        email,
    )
    return UserInDB(**row) if row else None


# --- Dependency for User Authentication ---
async def get_current_user(
    token: str = Depends(oauth2_scheme),
    conn: asyncpg.Connection = Depends(get_db_connection),
) -> UserInDB:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    row = await conn.fetchrow(
        """
        SELECT u.uuid, u.email, u.phone_number, u.name, u.hashed_password
        FROM tokens t
        JOIN users u ON u.uuid = t.user_uuid
        WHERE t.token = $1
          AND t.revoked_at IS NULL
          AND (t.expires_at IS NULL OR t.expires_at > NOW());
        """,
        token,
    )
    if not row:
        raise credentials_exception
    return UserInDB(**row)


# --- API Endpoints ---


@app.post("/api/auth/login", response_model=Token)
async def login_for_access_token(
    request: Request,
    body: LoginRequest,
    conn: asyncpg.Connection = Depends(get_db_connection),
):
    user = await get_user_by_email_from_db(conn, body.email)
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    user_agent = request.headers.get("User-Agent")
    ip_address = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For")
    token = await create_token(conn, user.uuid, user_agent, ip_address)
    return {"access_token": token}


@app.post("/api/auth/logout")
async def logout(
    request_obj: Request,
    response: Response,
    conn: asyncpg.Connection = Depends(get_db_connection),
    token: str = Depends(oauth2_scheme),
):
    await conn.execute(
        "UPDATE tokens SET revoked_at = NOW() WHERE token = $1",
        token,
    )
    return {"message": "Successfully logged out"}


@app.get("/auth/validate")
async def validate_token_for_nginx(
    token: str = Depends(oauth2_scheme),
    conn: asyncpg.Connection = Depends(get_db_connection),
):
    row = await conn.fetchrow(
        """
        SELECT t.user_uuid, u.email
        FROM tokens t
        JOIN users u ON u.uuid = t.user_uuid
        WHERE t.token = $1 AND t.revoked_at IS NULL
          AND (t.expires_at IS NULL OR t.expires_at > NOW())
        """,
        token,
    )
    if not row:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    return JSONResponse(
        content={"status": "ok"},
        headers={"X-User-ID": str(row["user_uuid"]), "X-Email": row["email"]},
    )


@app.post("/api/auth/register", response_model=User)
async def register_user(
    user_data: UserCreate, conn: asyncpg.Connection = Depends(get_db_connection)
):
    phone_number = user_data.phone_number.strip()
    if phone_number.startswith("0"):
        logger.warning(
            "Deprecated: phone_number '%s' uses local format. "
            "Pass E.164 format (e.g. +886%s) instead.",
            phone_number,
            phone_number[1:],
        )
        phone_number = "+886" + phone_number[1:]

    hashed_password = get_password_hash(user_data.password)
    try:
        new_user_row = await conn.fetchrow(
            """
            INSERT INTO users (uuid, name, email, phone_number, hashed_password)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING uuid, name, email, phone_number
            """,
            uuid.uuid4(),
            user_data.name,
            user_data.email,
            phone_number,
            hashed_password,
        )
        return User(**new_user_row)
    except asyncpg.exceptions.UniqueViolationError as e:
        constraint_name = e.constraint_name or ""
        if "users_email_key" in constraint_name:
            raise HTTPException(
                status_code=400, detail="An account with this email already exists."
            )
        if "users_phone_number_key" in constraint_name:
            raise HTTPException(
                status_code=400,
                detail="An account with this phone number already exists.",
            )
        raise HTTPException(
            status_code=400, detail="An account with this email already exists."
        )
    except Exception:
        raise HTTPException(
            status_code=500, detail="An unexpected error occurred during registration."
        )


@app.delete("/api/auth/user/{user_uuid}")
async def delete_user(
    user_uuid: uuid.UUID,
    current_user: UserInDB = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db_connection),
):
    if str(current_user.uuid) != str(user_uuid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to delete this account.",
        )
    await conn.execute("DELETE FROM users WHERE uuid = $1", user_uuid)
    return {"message": "User account deleted successfully."}


@app.get("/api/auth/status", response_model=User)
async def read_users_me(current_user: UserInDB = Depends(get_current_user)):
    """Return the authenticated user's details."""
    return current_user


# --- Device pairing (QR login) endpoints ---
#
# Session state lives in Redis (no DB table needed):
#   device:pair:{pairing_code}  → {device_code, device_label}  TTL=DEVICE_PAIRING_TTL_SECONDS
#   device:poll:{device_code}   → {status:"pending"} or {status:"approved",token:...}
#
# Flow:
#   1. Edge calls /pair → gets device_code (kept secret) + pairing_code (shown in QR).
#   2. Phone scans QR, calls /approve → pair key is atomically deleted (prevents re-use),
#      token is minted in postgres, poll key overwritten with token (TTL 30 s).
#   3. Edge polls /token → on "approved" the poll key is deleted and token returned once.
#      Key absence (TTL expired or already consumed) returns 404.


@app.post("/api/auth/device/pair", response_model=DevicePairResponse)
async def device_pair(
    r: aioredis.Redis = Depends(get_redis),
):
    device_code = secrets.token_urlsafe(32)
    pairing_code = secrets.token_urlsafe(16)
    await r.setex(
        f"device:pair:{pairing_code}",
        DEVICE_PAIRING_TTL_SECONDS,
        device_code,
    )
    await r.setex(
        f"device:poll:{device_code}",
        DEVICE_PAIRING_TTL_SECONDS,
        "pending",
    )
    return DevicePairResponse(
        device_code=device_code,
        pairing_code=pairing_code,
        expires_in=DEVICE_PAIRING_TTL_SECONDS,
        interval=DEVICE_POLL_INTERVAL_SECONDS,
    )


@app.post("/api/auth/device/approve")
async def device_approve(
    request: Request,
    body: DeviceApproveRequest,
    current_user: UserInDB = Depends(get_current_user),
    conn: asyncpg.Connection = Depends(get_db_connection),
    r: aioredis.Redis = Depends(get_redis),
):
    # Atomically consume the pairing_code (GETDEL prevents double-approval).
    device_code = await r.getdel(f"device:pair:{body.pairing_code}")
    if device_code is None:
        raise HTTPException(status_code=404, detail="Pairing code not found")

    user_agent = request.headers.get("User-Agent")
    ip_address = request.headers.get("X-Real-IP") or request.headers.get("X-Forwarded-For")
    token = await create_token(conn, current_user.uuid, user_agent, ip_address)

    # Give the edge 30 s to pick up the token on its next poll.
    await r.setex(f"device:poll:{device_code}", 30, token)
    return {"status": "approved"}


@app.post("/api/auth/device/token")
async def device_token(
    body: DeviceTokenRequest,
    r: aioredis.Redis = Depends(get_redis),
):
    raw = await r.get(f"device:poll:{body.device_code}")
    if raw is None:
        raise HTTPException(status_code=404, detail="Invalid device code")

    if raw == "pending":
        return {"status": "pending"}

    # It's the token — delete the key so it's delivered exactly once.
    await r.delete(f"device:poll:{body.device_code}")
    return {"status": "approved", "access_token": raw, "token_type": "bearer"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
