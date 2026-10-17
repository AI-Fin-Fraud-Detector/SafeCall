from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

from . import database
from .channels.base import Channel
from .channels.line import LineChannel
from .channels.line import router as line_router
from .events import RENDERERS

# Register new channels here (WhatsApp, mail, ...). Every event is delivered
# to every channel the user is bound to.
CHANNELS: list[Channel] = [LineChannel()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    try:
        yield
    finally:
        await database.close()


app = FastAPI(lifespan=lifespan)
app.include_router(line_router)


# Internal API: called service-to-service over the docker network only.
# nginx has no route for /internal/*, so it is unreachable from outside.
internal = APIRouter(prefix="/internal")


class NotifyRequest(BaseModel):
    user_id: str
    event: str


@internal.post("/notify")
async def notify(body: NotifyRequest):
    render = RENDERERS.get(body.event)
    if render is None:
        raise HTTPException(400, f"Unknown event type: {body.event!r}")
    user = await database.get_user(body.user_id)
    if user is None:
        raise HTTPException(404, "User not found.")

    text = render(user)
    delivered = []
    for channel in CHANNELS:
        try:
            if await channel.send(user, text):
                delivered.append(channel.name)
        except Exception as e:
            print(f"[notify] {channel.name} failed for {body.user_id}: {e}", flush=True)
    return {"delivered": delivered}


app.include_router(internal)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
