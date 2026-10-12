"""FastAPI entrypoint — wraps the Gemini vision+audio pipeline behind the same
5 endpoints the React Native frontend already calls (src/services/api.ts).

Run with: uvicorn api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import reel_jobs
from api.errors import register_error_handlers
from api.routes import help_center, reels, subscription, user

# uvicorn only configures its own loggers; give ours (reel cost, errors) a visible INFO handler.
_log = logging.getLogger("reelvault")
if not _log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Re-run reels that were queued or mid-run when the server last stopped; in a thread so startup (and the
    # health check Railway waits on) never waits for the database.
    threading.Thread(target=reel_jobs.recover_on_startup, name="reel-job-recovery", daemon=True).start()
    yield


app = FastAPI(title="ReelVault API (backend2 — Gemini vision pipeline)", lifespan=lifespan)

# Wildcard is intentional for local development across Expo's web/native
# targets. Narrow this to the deployed app's real origin in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

register_error_handlers(app)

app.include_router(reels.router)
app.include_router(user.router)
app.include_router(subscription.router)
app.include_router(help_center.router)


@app.get("/")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "reelvault-backend2"}
