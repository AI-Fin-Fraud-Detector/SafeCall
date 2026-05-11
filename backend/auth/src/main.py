import os
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from pydantic import BaseModel, ConfigDict

# --- Configuration ---
DB_DATABASE_NAME = os.getenv("DB_DATABASE_NAME")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")

DATABASE_URL = (
    f"postgresql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_DATABASE_NAME}"
)

# --- Password Hashing & Token URL ---
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# --- Database Pool ---
pool: Optional[asyncpg.Pool] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    try:
        pool = await asyncpg.create_pool(DATABASE_URL)
        print("Database pool created successfully.")
        yield
    finally:
        if pool:
            await pool.close()
            print("Database pool closed.")


async def get_db_connection():
    if pool is None:
        raise HTTPException(
            status_code=503, detail="Database connection is not available."
        )
    async with pool.acquire() as connection:
        yield connection


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


# --- Main Application Instance ---
app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Utility Functions ---
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password):
    return pwd_context.hash(password)


async def create_token(
    conn: asyncpg.Connection,
    user_uuid: uuid.UUID,
    user_agent: Optional[str],
    ip_address: Optional[str],
) -> str:
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
            user_data.phone_number,
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
    return current_user


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
