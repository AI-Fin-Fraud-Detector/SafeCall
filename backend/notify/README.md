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
| `GET /api/notify/line/login-url` | JWT (via nginx) | LINE Login authorize URL (with `bot_prompt=aggressive`) |
| `GET /api/notify/line/callback` | none (LINE redirect) | Exchange code, bind `users.line_id`, prompt to add friend |
| `GET /api/notify/line/status` | JWT (via nginx) | `{"bound": bool, "friend": bool, "add_friend_url": str \| null}` |

LINE accepts pushes to users who have not added (or have blocked) the official
account but never delivers them. The binding still succeeds, but the success
page shows an add-friend button and redirects to the add-friend screen, and
`status.friend` lets the app prompt a user who is not (or no longer) a friend.
Reloading the callback within 10 minutes re-shows the result, since returning
from LINE's add-friend screen can reload the already-used URL.
`bot_prompt` only takes effect when the LINE Login channel is linked to the
official account (LINE Developers console → LINE Login channel → Basic settings).

## Configuration

| Variable | Description |
|---|---|
| `LINE_LOGIN_CHANNEL_ID` / `LINE_LOGIN_CHANNEL_SECRET` | LINE Login channel |
| `LINE_LOGIN_REDIRECT_URI` | Public callback URL, must match the LINE console |
| `LINE_MESSAGING_CHANNEL_ACCESS_TOKEN` | Messaging API channel token |
| `DB_*`, `REDIS_URL` | Same as other services |
