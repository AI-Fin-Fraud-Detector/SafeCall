import html
import json
import os
import secrets
import uuid
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

from .. import database

LOGIN_CHANNEL_ID = os.getenv("LINE_LOGIN_CHANNEL_ID", "")
LOGIN_CHANNEL_SECRET = os.getenv("LINE_LOGIN_CHANNEL_SECRET", "")
LOGIN_REDIRECT_URI = os.getenv("LINE_LOGIN_REDIRECT_URI", "")
MESSAGING_ACCESS_TOKEN = os.getenv("LINE_MESSAGING_CHANNEL_ACCESS_TOKEN", "")

AUTHORIZE_URL = "https://access.line.me/oauth2/v2.1/authorize"
TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
USERINFO_URL = "https://api.line.me/oauth2/v2.1/userinfo"
PUSH_URL = "https://api.line.me/v2/bot/message/push"
BOT_INFO_URL = "https://api.line.me/v2/bot/info"
BOT_PROFILE_URL = "https://api.line.me/v2/bot/profile/{line_id}"
ADD_FRIEND_URL = "https://line.me/R/ti/p/{basic_id}"

OAUTH_STATE_TTL_SECONDS = 600


class LineChannel:
    name = "line"

    async def send(self, user: dict, text: str) -> bool:
        line_id = user.get("line_id")
        if not line_id:
            print("[LINE] User has no bound LINE account.", flush=True)
            return False
        if not MESSAGING_ACCESS_TOKEN:
            print("[LINE] Messaging channel access token is not configured.", flush=True)
            return False

        payload = {"to": line_id, "messages": [{"type": "text", "text": text}]}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    PUSH_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {MESSAGING_ACCESS_TOKEN}"},
                )
        except Exception as e:
            print(f"[LINE] Push error: {e}", flush=True)
            return False

        if 200 <= response.status_code < 300:
            return True
        print(f"[LINE] Push failed: {response.status_code} {response.text}", flush=True)
        return False


async def is_friend(line_id: str) -> bool:
    """Whether `line_id` has added the official account as a friend (and not
    blocked it). LINE accepts pushes to non-friends but never delivers them."""
    if not MESSAGING_ACCESS_TOKEN:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                BOT_PROFILE_URL.format(line_id=line_id),
                headers={"Authorization": f"Bearer {MESSAGING_ACCESS_TOKEN}"},
            )
    except Exception as e:
        print(f"[LINE] Friendship check error: {e}", flush=True)
        return False
    return response.status_code == 200


_add_friend_url: str | None = None


async def get_add_friend_url() -> str | None:
    """Add-friend link for the official account (cached after the first lookup)."""
    global _add_friend_url
    if _add_friend_url is None and MESSAGING_ACCESS_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    BOT_INFO_URL,
                    headers={"Authorization": f"Bearer {MESSAGING_ACCESS_TOKEN}"},
                )
            basic_id = response.json().get("basicId") if response.status_code == 200 else None
            if basic_id:
                _add_friend_url = ADD_FRIEND_URL.format(basic_id=basic_id)
        except Exception as e:
            print(f"[LINE] Bot info error: {e}", flush=True)
    return _add_friend_url


# ─── Account binding (LINE Login) ────────────────────────────────────────────
# nginx validates the JWT for login-url / status and injects X-User-ID.
# The callback is hit by LINE's redirect and carries no token (auth_request off).

router = APIRouter(prefix="/api/notify/line")


@router.get("/login-url")
async def get_login_url(x_user_id: str = Header(...)):
    state = secrets.token_urlsafe(32)
    await database.redis_client.setex(
        f"notify:line:oauth:{state}", OAUTH_STATE_TTL_SECONDS, x_user_id
    )
    params = {
        "response_type": "code",
        "client_id": LOGIN_CHANNEL_ID,
        "redirect_uri": LOGIN_REDIRECT_URI,
        "state": state,
        "scope": "openid profile",
        # Ask the user to add the official account as a friend during login.
        # Only shown when the LINE Login channel is linked to the official account.
        "bot_prompt": "aggressive",
    }
    return {"authorization_url": f"{AUTHORIZE_URL}?{urlencode(params)}"}


@router.get("/callback", response_class=HTMLResponse)
async def login_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_description: str | None = Query(None),
):
    if error:
        return HTMLResponse(
            f"<html><body><h2>LINE Binding Failed</h2>"
            f"<p>{html.escape(error)}</p>"
            f"<p>{html.escape(error_description or 'Unknown LINE Login error')}</p>"
            f"</body></html>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(400, "Missing LINE authorization code or state.")

    # Returning from LINE's add-friend screen can reload this URL after the state
    # and code are spent; re-show the result instead of an error.
    done_raw = await database.redis_client.get(f"notify:line:done:{state}")
    if done_raw is not None:
        done = json.loads(done_raw)
        return await _binding_success_page(done["line_user_id"], done["name"])

    # GETDEL: read the user UUID and invalidate the state in one step.
    user_uuid_raw = await database.redis_client.getdel(f"notify:line:oauth:{state}")
    if user_uuid_raw is None:
        raise HTTPException(400, "Invalid or expired LINE Login state.")
    try:
        user_uuid = str(uuid.UUID(user_uuid_raw))
    except ValueError:
        raise HTTPException(400, "Invalid user UUID in LINE state.")

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 1. authorization code -> LINE access token
        token_response = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": LOGIN_REDIRECT_URI,
                "client_id": LOGIN_CHANNEL_ID,
                "client_secret": LOGIN_CHANNEL_SECRET,
            },
        )
        if token_response.status_code != 200:
            print(f"[LINE] Token exchange failed: {token_response.text}", flush=True)
            raise HTTPException(502, "Failed to exchange LINE authorization code.")
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise HTTPException(502, "LINE access token was not returned.")

        # 2. LINE access token -> LINE user ID
        user_response = await client.get(
            USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
        if user_response.status_code != 200:
            print(f"[LINE] Userinfo failed: {user_response.text}", flush=True)
            raise HTTPException(502, "Failed to get LINE user information.")
        line_user = user_response.json()

    line_user_id = line_user.get("sub")
    if not line_user_id:
        raise HTTPException(502, "LINE user ID was not returned.")

    # 3. persist the binding
    if not await database.set_line_id(user_uuid, line_user_id):
        raise HTTPException(404, "SafeCall user not found.")
    print(f"[LINE] Bound user_uuid={user_uuid} line_user_id={line_user_id}", flush=True)

    name = line_user.get("name") or "LINE User"
    await database.redis_client.setex(
        f"notify:line:done:{state}",
        OAUTH_STATE_TTL_SECONDS,
        json.dumps({"line_user_id": line_user_id, "name": name}),
    )
    return await _binding_success_page(line_user_id, name)


async def _binding_success_page(line_user_id: str, name: str) -> HTMLResponse:
    """Success page. LINE silently drops pushes to non-friends, so a user who has
    not added the official account also gets an add-friend button and is sent to
    the add-friend screen automatically."""
    add_friend_url = None if await is_friend(line_user_id) else await get_add_friend_url()
    auto_redirect = ""
    add_friend_section = ""
    if add_friend_url:
        safe_url = html.escape(add_friend_url)
        auto_redirect = f'<meta http-equiv="refresh" content="2;url={safe_url}">'
        add_friend_section = f"""
    <p style="color:#c0392b;">Add the SafeCall official account as a friend to receive
       scam alerts on LINE.</p>
    <p><a href="{safe_url}"
          style="display:inline-block;padding:12px 24px;background:#06C755;
                 color:white;border-radius:8px;text-decoration:none;">Add SafeCall on LINE</a></p>"""

    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {auto_redirect}
  <title>LINE Binding Successful</title>
</head>
<body style="font-family: Arial, sans-serif; background: #fff8df; padding: 30px;">
  <div style="max-width: 480px; margin: 60px auto; background: white;
              padding: 28px; border-radius: 16px; text-align: center;">
    <div style="font-size: 48px; color: #06C755;">&#10003;</div>
    <h2>LINE Binding Successful</h2>
    <p>{html.escape(name)}</p>
    <p style="color:#777;">Your LINE account has been linked to your SafeCall account.</p>{add_friend_section}
    <p style="color:#999;font-size:14px;">You can now close this page and return to SafeCall.</p>
  </div>
</body>
</html>"""
    )


@router.get("/status")
async def get_binding_status(x_user_id: str = Header(...)):
    user = await database.get_user(x_user_id)
    line_id = user.get("line_id") if user else None
    return {
        "bound": bool(line_id),
        "friend": bool(line_id) and await is_friend(line_id),
        "add_friend_url": await get_add_friend_url(),
    }
