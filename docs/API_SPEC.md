# Auth Service — API Specification

Base URL (via nginx): `/api/auth`

Tokens are opaque random strings (`secrets.token_urlsafe(32)`) stored in the `tokens` table. A token remains valid until it is explicitly revoked via logout. Pass tokens in the `Authorization` header as a Bearer token.

---

## Public Endpoints

### POST /api/auth/register

Create a new user account.

> **Deprecated:** Local-format phone numbers (e.g. `0912345678`) are automatically converted to E.164 (`+886912345678`). Pass E.164 format directly — local format support will be removed in a future release.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `email` | string | Unique email address |
| `phone_number` | string | Unique phone number (E.164 format preferred, e.g. `+886912345678`) |
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

### Contacts APIs

These endpoints let the app manage the user's contact list in backend storage.
Incoming-call routing uses this list to classify callers (`contact` / `non_contact` / `private`).

All `/api/fraud/*` endpoints are behind nginx `auth_request`. After token validation, nginx injects `X-User-ID` and `X-Email` headers into the upstream request — clients should **not** send these manually. Clients only need to send:

```
Authorization: Bearer <access_token>
```

#### POST /api/fraud/contacts

Create a contact.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `name` | string | Contact display name |
| `phone_number` | string | Phone number in E.164 format (e.g. `+886912345678`) |

**Response `201`**

```json
{
  "id": "d8d2c1bd-4db1-4c3e-8ac4-f4af9df6f3bf",
  "name": "Alice",
  "phone_number": "+886912345678",
  "created_at": "2026-07-16T01:23:45.123456+00:00",
  "updated_at": "2026-07-16T01:23:45.123456+00:00"
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `name cannot be empty.` |
| `400` | `phone_number must be in E.164 format (e.g. +886912345678).` |
| `401` | Missing or invalid token |
| `409` | `This contact phone number already exists for the current user.` |
| `503` | `Database unavailable` |

#### GET /api/fraud/contacts

List all contacts for the authenticated user.

**Response `200`**

```json
[
  {
    "id": "d8d2c1bd-4db1-4c3e-8ac4-f4af9df6f3bf",
    "name": "Alice",
    "phone_number": "+886912345678",
    "created_at": "2026-07-16T01:23:45.123456+00:00",
    "updated_at": "2026-07-16T01:23:45.123456+00:00"
  }
]
```

**Errors**

| Status | Detail |
|---|---|
| `401` | Missing or invalid token |
| `503` | `Database unavailable` |

#### PUT /api/fraud/contacts/{contact_id}

Update a contact.

Same request body as create.

**Response `200`**

```json
{
  "id": "d8d2c1bd-4db1-4c3e-8ac4-f4af9df6f3bf",
  "name": "Alice",
  "phone_number": "+886912345678",
  "created_at": "2026-07-16T01:23:45.123456+00:00",
  "updated_at": "2026-07-16T01:30:00.000000+00:00"
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `name cannot be empty.` |
| `400` | `phone_number must be in E.164 format (e.g. +886912345678).` |
| `401` | Missing or invalid token |
| `404` | `Contact not found.` |
| `409` | `This contact phone number already exists for the current user.` |
| `503` | `Database unavailable` |

#### DELETE /api/fraud/contacts/{contact_id}

Delete a contact.

**Response `200`**

```json
{ "message": "Contact deleted successfully." }
```

**Errors**

| Status | Detail |
|---|---|
| `401` | Missing or invalid token |
| `404` | `Contact not found.` |
| `503` | `Database unavailable` |

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

# Fraud Detection Service — API Specification

Base URL (via nginx): `/api/fraud`

All `/api/fraud/*` endpoints are behind nginx `auth_request`. After token validation, nginx injects `X-User-ID` and `X-Email` headers into the upstream request — clients should **not** send these manually.

---

## Authenticated Endpoints

### POST /api/fraud/incoming-call

Register an incoming phone call.

Flow summary:
- Backend first checks whether caller phone exists in `/api/auth/contacts`.
- If caller is a saved contact, backend defaults to **direct call** path (no stranger-call fraud flow).
- Otherwise backend uses user setting `scam_detection` to decide fraud-detection vs direct-call path.

**Request headers**

| Header | Description |
|---|---|
| `X-Installation-Id` | Edge device installation ID |

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `phone_number` | string | Caller's phone number |
| `caller_name` | string \| null | Caller's name (optional) |

**Response `200` (fraud detection enabled, non-contact caller)**

```json
{
  "status": "ok",
  "fraud_detection": "enabled",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "caller_type": "non_contact"
}
```

After each trigger inference, an `ssci_update` push sends the full SSCI snapshot (`caller_type`, per-identity `scam_threshold`, and the cumulative scores so far) as the frontend renders them over time. Once a threshold is crossed (`scam_probability > scam_threshold`) — but only after `call_age >= SSCI_MAX_DURATION_SECONDS` (120s) and once per call — exactly one final alert push fires: either `fraud_alert` or `safe_to_answer`, keyed to the caller's identity threshold.

**Response `200` (direct call path: fraud disabled or caller is contact)**

```json
{
  "status": "ok",
  "fraud_detection": "disabled",
  "call_token": "abc123...",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "caller_type": "contact",
  "is_known_contact": true
}
```

A `direct_call` gRPC status is sent to the edge device; no SSCI computation occurs.

`call_token` can be consumed once via `POST /api/fraud/call/connect` to ensure direct-call handoff can be retried safely.

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header`
|---|---|

---

### POST /api/fraud/call/connect

Consume a one-time call token and trigger direct-call handoff to the active edge session.
Use this endpoint when app receives a `call_token` and wants to finalize direct call connection.
If edge session is not connected yet, backend returns `409` and **does not consume** the token.

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `call_token` | string | One-time token returned by `incoming-call` |

**Response `200`**

```json
{
  "status": "ok",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "caller_phone": "+886912345678",
  "caller_name": "Alice",
  "caller_type": "contact",
  "edge_session_ready": true
}
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header` |
| `400` | `Call token data is incomplete` |
| `403` | `Call token does not belong to this user` |
| `409` | `No active edge session. Keep call token and retry.` |
| `404` | `Invalid or expired call token` |

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

### GET /api/fraud/webrtc/offer

Fetch the WebRTC SDP offer the edge device produced after the user answered. Kebbi polls this (short retry) until the offer is ready, then sets it as its remote description.

**Response `200`** — one of:

```json
{ "status": "pending" }
```
```json
{ "status": "ready", "sdp": "v=0\r\n..." }
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header` |
| `404` | `No active edge session` |

---

### POST /api/fraud/webrtc/answer

Relay kebbi's WebRTC SDP answer to the edge device (over the gRPC `signal` channel). Edge applies it as its remote description and media flows P2P (edge ⇄ kebbi).

**Request body** (`application/json`)

| Field | Type | Description |
|---|---|---|
| `sdp` | string | The SDP answer generated by kebbi |

**Response `200`**

```json
{ "status": "ok" }
```

**Errors**

| Status | Detail |
|---|---|
| `400` | `Missing X-User-Id header` |
| `404` | `No active edge session` |

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
- **caller_type** — `contact` | `non_contact` | `private`, drives per-identity thresholds and the identity prior; may be `null` before the first caller-type is known
- **confidence** — raw confidence after evidence × agreement × stability; identity-prior adjusted
- **scam_probability** — probability of a scam decision at this trigger (= 1 − confidence)
- **evidence** — proportion of trigger inferences predicting scam (derived from `n_k`/`λ`, not the simple weighted mean shown here)
- **agreement** — consistency of historical predictions against the latest (beta-smoothed agreement estimate)
- **stability** — inverse of flip-count EMA over recent triggers; high = stable predictions
- **scam_threshold** — per-identity threshold this call uses (`contact` 0.40 / `non_contact` 0.50 / `private` 0.55)

Trigger fires every 3 inferences (`INFERENCES_PER_TRIGGER`, Δn=6). An **action** — either `fraud_alert` (when `scam_probability > scam_threshold`) or `safe_to_answer` — only fires **once per call**, gated by both a minimum call age ≥ `SSCI_MAX_DURATION_SECONDS` (120s) and the `_ssci_action_started` flag (`voice_session.py`).


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
- `signal` — WebRTC signaling, `{"kind": "offer", "sdp": "..."}`. After the user answers, edge stops sending `audio_chunk` and sends its SDP offer here; the fraud service stores it for `GET /api/fraud/webrtc/offer`.

**Server → Client (fraud service sends):**
- `audio_response` — TTS audio to play (16-bit PCM, 16kHz mono), chunked
- `signal` — WebRTC signaling, `{"kind": "answer", "sdp": "..."}`. Relayed from kebbi's `POST /api/fraud/webrtc/answer`; edge applies it as the remote description.
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
