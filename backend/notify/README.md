# Notify Service

Delivers user-facing notifications over external channels. Today: **LINE**.
Future channels (WhatsApp, mail, ...) plug in here too; web/mobile push stays in
`push_notification`.

## How it works

- Other services call the **internal** API over the docker network:
  `POST http://notify:8000/internal/notify` with `{"user_id": "<uuid>", "event": "scam_detected", "data": {...}}`. `data` is
  optional event context (for `scam_detected`: `caller_phone`, `caller_name`, `scam_probability`).
  nginx has no route for `/internal/*`, so it is not reachable from outside.
- The service renders the text (`src/events.py`) and sends it on every channel
  the user has bound (`CHANNELS` in `src/main.py`).
- Each channel implements `send(user, text)` (`src/channels/base.py`) and may
  expose its own account-binding routes under `/api/notify/<channel>/`, which
  nginx does forward (these must be browser/provider reachable).

## LINE endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/notify/line/login-url` | JWT (via nginx) | LINE Login authorize URL |
| `GET /api/notify/line/callback` | none (LINE redirect) | Exchange code, bind `users.line_id` |
| `GET /api/notify/line/status` | JWT (via nginx) | `{"bound": bool}` |

## Configuration

| Variable | Description |
|---|---|
| `LINE_LOGIN_CHANNEL_ID` / `LINE_LOGIN_CHANNEL_SECRET` | LINE Login channel |
| `LINE_LOGIN_REDIRECT_URI` | Public callback URL, must match the LINE console |
| `LINE_MESSAGING_CHANNEL_ACCESS_TOKEN` | Messaging API channel token |
| `DB_*`, `REDIS_URL` | Same as other services |
