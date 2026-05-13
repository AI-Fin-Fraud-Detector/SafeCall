# Push Notification Payload Spec

This document describes every push notification the backend sends to each app, and what the frontend is expected to do upon receiving it.

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

## App: `host_mobile`

> **TODO** — push notification handling for `host_mobile` is not yet implemented. This section will be filled in once the spec is confirmed.
