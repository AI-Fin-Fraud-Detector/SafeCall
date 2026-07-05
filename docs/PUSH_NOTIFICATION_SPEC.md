# Push Notification Service — Specification

## API Service

Base URL (via nginx): `/api/push`

All `/api/push/*` endpoints require `Authorization: Bearer <access_token>`.

### Authenticated Endpoints

#### POST /api/push/subscribe/{app}

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

#### GET /api/push/vapid_public_key

Return the VAPID public key needed to create a Web Push subscription on the client.

**Response `200`**

```json
{
  "public_key": "BNcRd..."
}
```

---

### Internal Endpoints

Not reachable from outside the cluster. Called by other backend services.

#### POST /notify

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

#### POST /notify/user/{user_id}

Send a notification to all devices of a specific user. Optionally scoped to one app.

**Path parameter:** `user_id` — target user UUID

**Request body:** same as `POST /notify` (including optional `app` field)

**Response `200`**

```json
{ "sent": 2, "failed": 0 }
```

---

### Redis Stream

Other services push notification events to the `push_notification_stream` Redis stream. The push notification service consumes this stream and delivers the notifications.

**Event fields**

| Field | Description |
|---|---|
| `callee_user_id` | UUID of the user to notify |
| `payload` | JSON-encoded notification payload |
| `app` | `host_mobile` \| `kebbi` — if set, only delivers to that app's tokens |

---

### Database Schema

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

## Notification Payloads

This section describes every push notification the backend sends to each app, and what the frontend is expected to do upon receiving it.

All notifications arrive as **FCM data messages** (or APNs equivalents). The `data` object is always a flat or nested JSON object. Fields listed as `silent: true` carry **no visible notification** — the app must handle them entirely in the background.

---

## App: `kebbi`

### 1. `incoming_call` — Fraud Detection **Enabled**

Sent by: `POST /api/fraud/incoming-call` when the user has fraud detection turned on.

| Field | Value |
|---|---|
| `title` | `"Incoming Call"` |
| `body` | `"Call from <phone_number>"` |
| `silent` | `false` (visible notification) |
| `android_priority` | _(default)_ |

**`data` payload**

```json
{
  "type": "incoming_call",
  "detail": {
    "phone_number": "+886912345678",
    "caller_name": "Alice",
    "conversation_id": "550e8400-e29b-41d4-a716-446655440000"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `"incoming_call"` | Notification type identifier |
| `detail.phone_number` | string | Caller's phone number. Empty string `""` means private / no caller ID |
| `detail.caller_name` | string \| null | Caller's name from the host's contact book. `null` if not saved or private number |
| `detail.conversation_id` | string (UUID) | The conversation created for this call. Use this to subscribe to real-time message pushes and to fetch the transcript |

**Expected behaviour**

- Show an incoming call UI.
- Store `conversation_id` to correlate subsequent `call_new_message` / `call_update_message` / `call_delete_message` pushes with this call.
- When the call is handled, use `conversation_id` to fetch the transcript via `GET /api/fraud/conversations/{conversation_id}/messages`.

---

### 2. `incoming_call` — Fraud Detection **Disabled**

Sent by: `POST /api/fraud/incoming-call` when the user has fraud detection turned off.

| Field | Value |
|---|---|
| `title` | `"Incoming Call"` |
| `body` | `"Call from <phone_number>"` |
| `silent` | `true` |
| `android_priority` | `"high"` |

**`data` payload**

```json
{
  "type": "incoming_call",
  "detail": {
    "phone_number": "+886912345678"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `"incoming_call"` | Notification type identifier |
| `detail.phone_number` | string | Caller's phone number. Empty string `""` means private / no caller ID |

> **Note:** No `conversation_id` or `caller_name` is included because no AI session is started. No real-time transcript pushes will follow.

**Expected behaviour**

- Wake up the app silently and alert the user that a direct call is happening.

---

### 3. `call_new_message`

Sent by: the fraud detection session in real time as speech is transcribed or the AI generates a response.

| Field | Value |
|---|---|
| `silent` | `true` |
| `android_priority` | _(default)_ |

**`data` payload**

```json
{
  "type": "call_new_message",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": {
    "id": "660e8400-e29b-41d4-a716-446655440000",
    "role": "user",
    "content": "Hello, who is this?",
    "metadata": null
  }
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `"call_new_message"` | Notification type identifier |
| `conversation_id` | string (UUID) | The active conversation |
| `message.id` | string (UUID) | Stable message ID. Use this for subsequent update/delete matching |
| `message.role` | `"user"` \| `"assistant"` | `user` = caller's speech; `assistant` = AI response |
| `message.content` | string | Transcribed or generated text |
| `message.metadata` | object \| null | Reserved for future use |

**Expected behaviour**

- Append the message to the live transcript view for the matching `conversation_id`.

---

### 4. `call_update_message`

Sent when a previously pushed user message is corrected (e.g. STT finalises a partial transcript).

| Field | Value |
|---|---|
| `silent` | `true` |
| `android_priority` | _(default)_ |

**`data` payload**

```json
{
  "type": "call_update_message",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": {
    "id": "660e8400-e29b-41d4-a716-446655440000",
    "role": "user",
    "content": "Hello, who is this calling?",
    "metadata": null
  }
}
```

Same fields as `call_new_message`.

**Expected behaviour**

- Find the message by `message.id` in the current transcript and replace its `content` in-place.

---

### 5. `call_delete_message`

Sent when one or more stale messages are removed (e.g. intermediate partial transcripts superseded by a new utterance).

| Field | Value |
|---|---|
| `silent` | `true` |
| `android_priority` | _(default)_ |

**`data` payload**

```json
{
  "type": "call_delete_message",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": {
    "id": "660e8400-e29b-41d4-a716-446655440000"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `"call_delete_message"` | Notification type identifier |
| `conversation_id` | string (UUID) | The active conversation |
| `message.id` | string (UUID) | ID of the message to remove |

**Expected behaviour**

- Remove the message with the matching `message.id` from the live transcript view.

---

### Message Ordering Notes

The three real-time message pushes (`call_new_message`, `call_update_message`, `call_delete_message`) may arrive out of order on mobile. The recommended reconciliation strategy:

1. Buffer incoming pushes by `conversation_id`.
2. Maintain a local map of `message.id → content`.
3. Apply `new` → insert, `update` → replace, `delete` → remove.
4. If a push references an unknown `message.id` on `update` or `delete`, silently discard it (the message was likely never received).
5. After a call ends, do a final fetch from `GET /api/fraud/conversations/{conversation_id}/messages` to reconcile any missed pushes.

---

### 6. `ssci_update`

Sent by: fraud detection session whenever SSCI is computed (trigger boundary).

| Field | Value |
|---|---|
| `silent` | `true` |
| `android_priority` | _(default)_ |

**`data` payload**

```json
{
  "type": "ssci_update",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "message_id": "660e8400-e29b-41d4-a716-446655440000",
  "ssci": { "trigger_index": 1, "scam_probability": 0.15, "caller_type": "non_contact", "scam_threshold": 0.5, ... }
}
```

**Expected behaviour**

- Update the real-time SSCI display with the latest fraud scoring data.

---

### 7. `fraud_alert`

Sent by: fraud detection session once per call when `scam_probability` exceeds the per-identity threshold (`ssci.scam_threshold`: contact 0.40 / non_contact 0.50 / private 0.55).

| Field | Value |
|---|---|
| `silent` | `true` |
| `android_priority` | `"high"` |

**`data` payload**

```json
{
  "type": "fraud_alert",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "scam_probability": 0.75,
  "ssci": { "trigger_index": 1, ... }
}
```

**Expected behaviour**

- Alert the user and encourage them to hang up. LLM continues naturally for 30 seconds (grace period), then stops. Call auto-ends after 90 total seconds if user doesn't answer.

---

### 8. `safe_to_answer`

Sent by: fraud detection session once per call when `scam_probability` does not exceed the per-identity threshold (`ssci.scam_threshold`: contact 0.40 / non_contact 0.50 / private 0.55).

| Field | Value |
|---|---|
| `silent` | `false` |
| `android_priority` | `"high"` |

**`data` payload**

```json
{
  "type": "safe_to_answer",
  "conversation_id": "550e8400-e29b-41d4-a716-446655440000",
  "scam_probability": 0.20,
  "ssci": { "trigger_index": 1, ... }
}
```

**Expected behaviour**

- Suggest that the user answer the call. LLM continues for 180 seconds (3 min). Call auto-ends if user doesn't answer.

---

## App: `host_mobile`

### 1. `hangup` (call_end)

Sent by: fraud detection session when call ends due to SSCI timeout or manual hangup.

| Field | Value |
|---|---|
| `silent` | `true` |
| `android_priority` | `"high"` |

**Payload**

```json
{
  "silent": true,
  "android_priority": "high",
  "data": {
    "action": "hangup"
  }
}
```

**Expected behaviour**

- End the active call session and return to the main screen.

---

> **TODO** — additional push notification handling for `host_mobile` is not yet implemented. This section will be filled in once the spec is confirmed.
