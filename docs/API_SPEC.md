# Auth Service — API Specification

Base URL (via nginx): `/api/auth`

Tokens are opaque random strings (`secrets.token_urlsafe(32)`) stored in the `tokens` table. A token remains valid until it is explicitly revoked via logout. Pass tokens in the `Authorization` header as a Bearer token.

---

## Public Endpoints

### POST /api/auth/register

Create a new user account.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `email` | string | Unique email address |
| `phone_number` | string | Unique phone number |
| `name` | string | Display name |
| `password` | string | Plain-text password (hashed with argon2id) |

**Response `201`**

```json
{
  "uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "email": "alice@example.com",
  "phone_number": "+886912345678",
  "name": "Alice"
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `An account with this email already exists.` |
| `400` | `An account with this phone number already exists.` |
| `500` | Unexpected server error |

---

### POST /api/auth/login

Authenticate and receive an access token.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `email` | string | Registered email address |
| `password` | string | Account password |

**Response `200`**

```json
{
  "access_token": "dGhpcyBpcyBhIHNhbXBsZSB0b2tlbg",
  "token_type": "bearer"
}
```

**Errors**

| Status | Detail |
|---|---|
| `401` | `Incorrect email or password` |

---

## Authenticated Endpoints

All requests must include:

```
Authorization: Bearer <access_token>
```

### POST /api/auth/logout

Revoke the current token. The token is immediately invalidated in the database.

**Response `200`**

```json
{
  "message": "Successfully logged out"
}
```

**Errors**

| Status | Detail |
|---|---|
| `401` | Missing or invalid token |

---

### GET /api/auth/status

Return the profile of the currently authenticated user.

**Response `200`**

```json
{
  "uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "email": "alice@example.com",
  "phone_number": "+886912345678",
  "name": "Alice"
}
```

**Errors**

| Status | Detail |
|---|---|
| `401` | Missing or invalid token |

---

## Internal Endpoints

These endpoints are called by nginx only and are not reachable from outside the cluster.

### GET /auth/validate

Validates a Bearer token and returns the user identity as response headers. Called by nginx `auth_request` before forwarding any `/api/*` request.

**Request headers**

```
Authorization: Bearer <access_token>
```

**Response `200`**

```json
{ "status": "ok" }
```

| Response header | Value |
|---|---|
| `X-User-ID` | User UUID |
| `X-Email` | Email string |

**Errors**

| Status | Detail |
|---|---|
| `401` | `Invalid or expired token` |

---

## Token Lifecycle

```
POST /api/auth/login
  → token created in DB (tokens table)
  → returned to client as access_token

Every /api/* request (except /api/auth/)
  → nginx calls GET /auth/validate internally
  → X-User-ID and X-Email injected into upstream request

POST /api/auth/logout
  → token revoked in DB (revoked_at = NOW())
  → subsequent requests with the same token return 401
```

---

## Database Schema

```sql
tokens (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_uuid    UUID REFERENCES users(uuid) ON DELETE CASCADE,
  token        TEXT UNIQUE NOT NULL,
  expires_at   TIMESTAMPTZ,          -- NULL means no expiry
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
)
```

---

# Push Notification Service — API Specification

Base URL (via nginx): `/api/push`

All `/api/push/*` endpoints require `Authorization: Bearer <access_token>`.

---

## Authenticated Endpoints

### POST /api/push/subscribe/{app}

Register a device push token for the authenticated user.

**Path parameter**

| Parameter | Values | Description |
|---|---|---|
| `app` | `host_mobile` \| `kebbi` | The app registering the token |

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `platform` | `"web"` \| `"fcm"` \| `"apns"` | Push delivery platform |
| `pushSubscription` | object \| null | Web Push subscription object (platform `web`) |
| `fcm_token` | string \| null | FCM device token (platform `fcm`) |
| `apns_token` | string \| null | APNs device token (platform `apns`) |

Exactly one of `pushSubscription`, `fcm_token`, or `apns_token` must be provided.

**Web Push example:**
```json
{
  "platform": "web",
  "pushSubscription": {
    "endpoint": "https://fcm.googleapis.com/fcm/send/...",
    "keys": {
      "p256dh": "BNcRd...",
      "auth": "tBHI..."
    }
  }
}
```

**FCM example:**
```json
{
  "platform": "fcm",
  "fcm_token": "fGH3k..."
}
```

**Response `200`**

```json
{
  "message": "Subscription saved!",
  "user_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `X-User-Id header is required.` |
| `400` | `Either pushSubscription, fcm_token, or apns_token must be provided` |

---

### GET /api/push/vapid_public_key

Return the VAPID public key needed to create a Web Push subscription on the client.

**Response `200`**

```json
{
  "public_key": "BNcRd..."
}
```

---

## Internal Endpoints

Not reachable from outside the cluster. Called by other backend services.

### POST /notify

Send a notification to all registered devices. Optionally scoped to one app.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `title` | string \| null | Notification title |
| `body` | string \| null | Notification body |
| `icon` | string \| null | Icon URL |
| `tag` | string \| null | Notification tag |
| `data` | object \| null | Custom data payload |
| `silent` | boolean | Send data-only (no visible notification), default `false` |
| `android_priority` | string \| null | FCM Android priority (`"high"` / `"normal"`) |
| `app` | `"host_mobile"` \| `"kebbi"` \| null | If set, only deliver to tokens registered for this app |

**Response `200`**

```json
{ "sent": 12, "failed": 1 }
```

---

### POST /notify/user/{user_id}

Send a notification to all devices of a specific user. Optionally scoped to one app.

**Path parameter:** `user_id` — target user UUID

**Request body:** same as `POST /notify` (including optional `app` field)

**Response `200`**

```json
{ "sent": 2, "failed": 0 }
```

---

## Redis Stream

Other services push notification events to the `push_notification_stream` Redis stream. The push notification service consumes this stream and delivers the notifications.

**Event fields**

| Field | Description |
|---|---|
| `callee_user_id` | UUID of the user to notify |
| `payload` | JSON-encoded notification payload |
| `app` | `host_mobile` \| `kebbi` — if set, only delivers to that app's tokens |

---

## Database Schema

```sql
push_notification (
  endpoint     TEXT PRIMARY KEY,
  userid       UUID REFERENCES users(uuid) ON DELETE CASCADE,
  expiration_time TIMESTAMP,
  p256dh       TEXT,
  auth         TEXT,
  app          TEXT NOT NULL,   -- 'host_mobile' | 'kebbi'
  platform     TEXT,            -- 'fcm' | 'apns' | 'webpush'
  created_at   TIMESTAMP DEFAULT NOW(),
  updated_at   TIMESTAMP DEFAULT NOW()
)
```
