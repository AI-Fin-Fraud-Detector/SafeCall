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

## Device Pairing (QR Login) Endpoints

Used to log in a headless edge device (e.g. a Kebbi robot) that has no keyboard.
Session state lives entirely in Redis — no Postgres table is used.

All three endpoints live under `/api/auth/` and therefore **bypass nginx token
validation** — the edge device is unauthenticated until pairing completes, and
the approve step authenticates itself with a Bearer token.

```
edge: POST /api/auth/device/pair          ──▶  { device_code, pairing_code }
edge: show QR encoding `pairing_code`
edge: POST /api/auth/device/token (poll)  ──▶  { status: "pending" }
phone: POST /api/auth/device/approve      ──▶  pairing_code key deleted; token minted
edge: POST /api/auth/device/token (poll)  ──▶  { status: "approved", access_token }
                                               (poll key deleted — token delivered once)
edge: POST /api/auth/device/token (again) ──▶  404 (key gone)
```

Redis keys:
- `device:pair:{pairing_code}` — exists while pending; TTL = `DEVICE_PAIRING_TTL_SECONDS`.
  Deleted atomically on `/approve` (prevents re-use).
- `device:poll:{device_code}` — pending for the full TTL; overwritten with `{approved, token}`
  for 30 s after approval; deleted when the edge picks up the token.

---

### POST /api/auth/device/pair

Start a pairing session. Called by the edge device. **No authentication.**

No request body.

**Response `200`**

```json
{
  "device_code": "mi-Vl37mTcCqWqMS-pVhKBw6IsO0C4YjPyTcExZ6lD4",
  "pairing_code": "_FgqVCQ8kif61ON2WfaKWg",
  "expires_in": 60,
  "interval": 3
}
```

| Field | Description |
|---|---|
| `device_code` | Secret the edge device keeps and polls with. Never shown to the user. |
| `pairing_code` | Short code the edge device encodes into its QR. The phone sends this to approve. |
| `expires_in` | Seconds until the pairing session expires. |
| `interval` | Suggested seconds between polls of `/api/auth/device/token`. |

---

### POST /api/auth/device/approve

Approve a pairing session. Called by the host_mobile app after scanning the QR.
**Requires `Authorization: Bearer <access_token>`** of the user the device is
being linked to.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `pairing_code` | string | The code read from the QR code. |

**Response `200`**

```json
{ "status": "approved" }
```

On success the `pairing_code` Redis key is deleted (preventing re-use) and a new
token is created in the `tokens` table for the authenticated user.

**Errors**

| Status | Detail |
|---|---|
| `401` | Missing or invalid Bearer token |
| `404` | `Pairing code not found` — key expired or already used |

---

### POST /api/auth/device/token

Poll for the device's token. Called repeatedly by the edge device. **No authentication.**

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `device_code` | string | The `device_code` returned by `/api/auth/device/pair`. |

**Response `200`** — one of:

```json
{ "status": "pending" }
```
```json
{ "status": "approved", "access_token": "dGhpcyBpcyBh...", "token_type": "bearer" }
```

The `access_token` is returned **exactly once**: the poll key is deleted immediately
after delivery. Subsequent polls return `404`. The edge device should cache the token.

**Errors**

| Status | Detail |
|---|---|
| `404` | `Invalid device code` |

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

POST /api/auth/device/pair  → device/approve → device/token  (QR login)
  → session state in Redis; token created in DB (tokens table)
  → delivered to the edge device on its next poll; Redis key deleted

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
  user_agent   TEXT,
  ip_address   VARCHAR(45),
  user_uuid    UUID REFERENCES users(uuid) ON DELETE CASCADE,
  token        TEXT UNIQUE NOT NULL,
  expires_at   TIMESTAMPTZ,          -- NULL means no expiry
  created_at   TIMESTAMPTZ DEFAULT NOW(),
  last_used_at TIMESTAMPTZ,
  revoked_at   TIMESTAMPTZ
)

-- Device pairing sessions live in Redis, not Postgres.
-- Redis keys (both auto-expire via TTL):
--   device:pair:{pairing_code} → {device_code}  (pending; deleted on approval)
--   device:poll:{device_code}  → {status, token} (deleted after token delivery)
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

---

# Fraud Detection Service — API Specification

Base URL (via nginx): `/api/fraud`

All `/api/fraud/*` endpoints are behind nginx `auth_request`. After token validation, nginx injects `X-User-ID` and `X-Email` headers into the upstream request — clients should **not** send these manually.

---

## Authenticated Endpoints

### POST /api/fraud/incoming-call

Register an incoming phone call. Creates a conversation and optionally triggers fraud detection (if enabled for the user).

**Request headers**

| Header | Description |
|---|---|
| `X-Installation-Id` | Edge device installation ID |

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `phone_number` | string | Caller's phone number |
| `caller_name` | string \| null | Caller's name (optional) |

**Response `200` (fraud detection enabled)**

```json
{
  "status": "ok",
  "fraud_detection": "enabled"
}
```

A `fraud_alert` or `safe_to_answer` push is sent to `host_mobile` once SSCI computation completes (after 3 inferences, minimum 60s into call).

**Response `200` (fraud detection disabled)**

```json
{
  "status": "ok",
  "fraud_detection": "disabled",
  "call_token": "abc123..."
}
```

A `direct_call` gRPC status is sent to the edge device; no SSCI computation occurs.

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header`
|---|---|

---

### POST /api/fraud/answer-call

User answered the call; stops LLM/TTS and hands off to edge for WebRTC.

Cancels the SSCI action timer (if running) and sends a `direct_call` gRPC status to the edge device.

**Response `200`**

```json
{ "status": "ok" }
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header` |

---

### POST /api/fraud/call-end

Call has ended; edge device should stop the session.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| (none) | — | Uses `X-User-Id` from headers |

**Response `200`**

```json
{ "status": "ok" }
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header` |

---

### GET /api/fraud/conversations

List the authenticated user's phone conversations, most recent first. Supports cursor-based pagination.

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `before` | string (UUID) | — | Cursor: return only conversations before this ID |
| `limit` | integer | `50` | Page size (1–200) |

**Response `200`**

```json
{
  "conversations": [
    {
      "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
      "title": null,
      "metadata": { "caller_phone_number": "+886912345678" },
      "created_at": "2026-05-12T10:30:00+00:00",
      "updated_at": "2026-05-12T10:35:00+00:00"
    }
  ],
  "next_cursor": "550e8400-e29b-41d4-a716-446655440001"
}
```

`next_cursor` is `null` when there are no more pages.

**Errors**

| Status | Detail |
|---|---|
| `400` | `limit must be between 1 and 200` |
| `400` | `Invalid before cursor` |
| `503` | `Database unavailable` |

---

### GET /api/fraud/conversations/{conversation_id}

Get a single conversation's metadata.

**Path parameter**

| Parameter | Type | Description |
|---|---|---|
| `conversation_id` | string (UUID) | Conversation UUID |

**Response `200`**

```json
{
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "title": null,
  "metadata": { "caller_phone_number": "+886912345678" },
  "created_at": "2026-05-12T10:30:00+00:00",
  "updated_at": "2026-05-12T10:35:00+00:00"
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Invalid conversation_id` |
| `404` | `Conversation not found` |
| `503` | `Database unavailable` |

---

### GET /api/fraud/conversations/{conversation_id}/messages

Get messages for a conversation (excluding system messages), in chronological order. Supports cursor-based pagination.

**Path parameter**

| Parameter | Type | Description |
|---|---|---|
| `conversation_id` | string (UUID) | Conversation UUID |

**Query parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `before` | string (UUID) | — | Cursor: return only messages before this ID |
| `limit` | integer | `50` | Page size (1–200) |

**Response `200`**

```json
{
  "messages": [
    {
      "id": "660e8400-e29b-41d4-a716-446655440000",
      "role": "user",
      "content": "Hello?",
      "metadata": {},
      "created_at": "2026-05-12T10:30:05+00:00",
      "edited_at": null
    }
  ],
  "next_cursor": "660e8400-e29b-41d4-a716-446655440001"
}
```

`next_cursor` is `null` when there are no more pages.

**Errors**

| Status | Detail |
|---|---|
| `400` | `Invalid conversation_id` |
| `400` | `limit must be between 1 and 200` |
| `400` | `Invalid before cursor` |
| `404` | `Conversation not found` |
| `503` | `Database unavailable` |

---

### GET /api/fraud/conversations/{conversation_id}/messages/{message_id}

Get a single message by ID.

**Path parameters**

| Parameter | Type | Description |
|---|---|---|
| `conversation_id` | string (UUID) | Conversation UUID |
| `message_id` | string (UUID) | Message UUID |

**Response `200`**

```json
{
  "id": "660e8400-e29b-41d4-a716-446655440000",
  "role": "user",
  "content": "Hello?",
  "metadata": {},
  "created_at": "2026-05-12T10:30:05+00:00",
  "edited_at": null
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Invalid conversation_id or message_id` |
| `404` | `Message not found` |
| `503` | `Database unavailable` |

---

## Message Metadata Schema

Messages include a `metadata` field (JSONB) with fraud detection results. This field is populated during conversation flow:

### Non-trigger inference (every message):
```json
{
  "detection": {
    "prediction": true,
    "inference_index": 2
  }
}
```

### Trigger inference (every 3rd inference):
```json
{
  "detection": {
    "prediction": false,
    "inference_index": 3,
    "ssci": {
      "trigger_index": 1,
      "confidence": 0.9134559,
      "scam_probability": 0.0865441,
      "evidence": 0.67,
      "agreement": 0.89,
      "stability": 0.98
    }
  }
}
```

### SSCI Scoring

- **evidence** — proportion of trigger inferences predicting scam (0.0 = all safe, 1.0 = all scam)
- **agreement** — consistency of predictions (1.0 = unanimous, lower = disagreement)
- **stability** — inverse of flip count EMA; how stable predictions are over time
- **scam_probability** — `evidence*0.5 + agreement*0.3 + stability*0.2`
- **confidence** — `1.0 - scam_probability`

Trigger fires every 3 inferences (`INFERENCES_PER_TRIGGER`), but SSCI action (alert/safe-to-answer) only fires **once per call**, after minimum call age ≥ 60s (`SSCI_MAX_DURATION_SECONDS`).

---

## Push Notification Payloads

Fraud service sends the following push notification types to `host_mobile`:

### ssci_update

Sent whenever SSCI is computed (trigger boundary).

```json
{
  "silent": true,
  "data": {
    "type": "ssci_update",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
    "message_id": "660e8400-e29b-41d4-a716-446655440000",
    "ssci": { "trigger_index": 1, "scam_probability": 0.15, ... }
  }
}
```

### fraud_alert

Sent once per call when `scam_probability > 0.6`. App should alert user and encourage them to hang up. LLM continues naturally for 30 seconds (grace period), then stops. Call auto-ends after 90 total seconds if user doesn't answer.

```json
{
  "silent": true,
  "android_priority": "high",
  "data": {
    "type": "fraud_alert",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
    "scam_probability": 0.75,
    "ssci": { "trigger_index": 1, ... }
  }
}
```

### safe_to_answer

Sent once per call when `scam_probability < 0.6`. App should suggest user answer the call. LLM continues for 180 seconds (3 min). Call auto-ends if user doesn't answer.

```json
{
  "silent": false,
  "android_priority": "high",
  "data": {
    "type": "safe_to_answer",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
    "scam_probability": 0.20,
    "ssci": { "trigger_index": 1, ... }
  }
}
```

### call_new_message

Sent when a new message is added to the conversation.

```json
{
  "silent": true,
  "data": {
    "type": "call_new_message",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
    "message": {
      "id": "660e8400-e29b-41d4-a716-446655440000",
      "role": "user",
      "content": "Hello?",
      "metadata": {}
    }
  }
}
```

### call_update_message

Sent when a message's content or metadata is updated (e.g., SSCI score added).

```json
{
  "silent": true,
  "data": {
    "type": "call_update_message",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
    "message": { "id": "660e8400-..." }
  }
}
```

---

### GET /health

Health check endpoint. Returns service status and component health.

**Response `200`**

```json
{
  "status": "healthy",
  "service": "anti-fraud",
  "components": {
    "redis": "healthy",
    "grpc": "listening on :60015",
    "active_sessions": 2
  }
}
```

---

## gRPC Streaming (Internal)

Edge devices maintain a bidirectional gRPC stream on port 60015 via `VoiceService.Session()`.

**Client → Server (edge sends):**
- `audio_chunk` — raw audio bytes from microphone (16-bit PCM, 16kHz mono)
- `interrupt` — VAD detected speech during playback; stop TTS immediately

**Server → Client (fraud service sends):**
- `audio_response` — TTS audio to play (16-bit PCM, 16kHz mono), chunked
- `text_status` — call lifecycle and metadata events:
  - `incoming_call` — incoming phone call metadata (caller_phone, caller_name)
  - `direct_call` — user answered; hand off to WebRTC
  - `call_end` — call ended; close session
  - `stt_result` — transcribed user speech (interim or final)
  - `llm_response` — LLM response text
  - `playback_complete` — final audio chunk sent, clear queue

Metadata sent via headers:
- `authorization` — edge device access token (Bearer)

---

## Database Schema

```sql
conversations (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  type       TEXT NOT NULL CHECK (type IN ('phone', 'chat')),
  user_uuid  UUID NOT NULL REFERENCES users(uuid) ON DELETE CASCADE,
  title      TEXT,
  metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)

messages (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
  content         TEXT NOT NULL,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  edited_at       TIMESTAMPTZ
)
```
