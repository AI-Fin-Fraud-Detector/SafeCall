# Authentication Service

Handles user registration, login, logout, and token validation for the SafeCall backend.

## Technologies

- **FastAPI** — web framework
- **asyncpg** — async PostgreSQL driver
- **Passlib / argon2-cffi** — password hashing
- **python-dotenv** — environment variable loading
- **Uvicorn** — ASGI server

## Configuration

| Variable | Description |
|---|---|
| `DB_DATABASE_NAME` | PostgreSQL database name |
| `DB_USERNAME` | PostgreSQL connection user |
| `DB_PASSWORD` | PostgreSQL connection password |
| `DB_HOST` | PostgreSQL host |
| `DB_PORT` | PostgreSQL port (default: `5432`) |

## API Endpoints

All public endpoints are prefixed with `/api/auth`. See `docs/API_SPEC.md` in the repo root for the full specification.

### POST /api/auth/register

**Request:**
```json
{
  "email": "user@example.com",
  "phone_number": "0987654321",
  "name": "Alice",
  "password": "securepassword123"
}
```

**Response:**
```json
{
  "uuid": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
  "email": "user@example.com",
  "phone_number": "0987654321",
  "name": "Alice"
}
```

### POST /api/auth/login

**Request:**
```json
{
  "email": "user@example.com",
  "password": "securepassword123"
}
```

**Response:**
```json
{
  "access_token": "dGhpcyBpcyBhIHNhbXBsZSB0b2tlbg",
  "token_type": "bearer"
}
```

### POST /api/auth/logout

Requires `Authorization: Bearer <token>`. Revokes the token immediately.

### GET /api/auth/status

Requires `Authorization: Bearer <token>`. Returns the current user's profile.

### GET /auth/validate *(internal)*

Called only by nginx `auth_request`. Returns `X-User-ID` and `X-Email` headers used to authenticate downstream services.

## curl Examples

```bash
# Register
curl -X POST "http://localhost:8100/api/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","phone_number":"0987654321","name":"Alice","password":"securepassword123"}'

# Login
curl -X POST "http://localhost:8100/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"securepassword123"}'

# Status
curl -X GET "http://localhost:8100/api/auth/status" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"

# Logout
curl -X POST "http://localhost:8100/api/auth/logout" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN"
```
