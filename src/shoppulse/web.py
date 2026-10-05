"""HTTP: сторінка дашборду, JSON для неї і питання до консультанта.

База будується в пам'яті при першому запиті (детерміновано, <1 с), тож деплой
не тягне файл бази і всі інстанси показують однакові цифри.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from shoppulse import alerts, assistant, metrics, world

STATIC = Path(__file__).resolve().parents[2] / "static"
PER_IP_PER_HOUR = 10
GLOBAL_PER_DAY = 200

app = FastAPI(title="Shop Pulse", docs_url=None, redoc_url=None)
_lock = threading.Lock()
_conn = None
_hits: dict[str, deque] = {}
_day_count = {"day": "", "n": 0}


def conn():
    global _conn
    with _lock:
        if _conn is None:
            _conn = world.build()
        return _conn


def _allow(ip: str, now: float | None = None) -> str | None:
    """Ліміти на консультанта: ключ моделі платний, демо публічне."""
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _lock:
        if _day_count["day"] != today:
            _day_count.update(day=today, n=0)
        if _day_count["n"] >= GLOBAL_PER_DAY:
            return "Денний ліміт питань до демо вичерпано, спробуйте завтра."
        q = _hits.setdefault(ip, deque(maxlen=PER_IP_PER_HOUR))
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= PER_IP_PER_HOUR:
            return "Не більше 10 питань на годину з однієї адреси."
        q.append(now)
        _day_count["n"] += 1
        if len(_hits) > 5000:           # пам'ять не росте без меж
            _hits.clear()
    return None


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/og.png")
def og() -> FileResponse:
    return FileResponse(STATIC / "og.png")


@app.get("/api/dashboard")
def dashboard(days: int = 30) -> dict:
    if days not in (7, 30, 90):
        raise HTTPException(400, "days must be 7, 30 or 90")
    c = conn()
    return {
        "as_of": world.END.isoformat(),
        "kpis": metrics.kpis(c, days),
        "daily": metrics.daily(c, max(days, 30) if days != 7 else 30),
        "top": metrics.top_products(c, days, 8),
        "stock": metrics.stock_cover(c, max_days=7, limit=8),
        "channels": metrics.channels(c, days),
        "meta_weekly": metrics.channel_weekly(c, "meta_ads", 8),
        "google_weekly": metrics.channel_weekly(c, "google_ads", 8),
        "alerts": alerts.build_alerts(c),
    }


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=assistant.MAX_QUESTION_CHARS)


@app.post("/api/ask")
def ask(body: AskIn, request: Request) -> JSONResponse:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        return JSONResponse({"error": "Консультант вимкнений: на сервері не задано ключ моделі."}, 503)
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "?")).split(",")[0]
    denied = _allow(ip.strip())
    if denied:
        return JSONResponse({"error": denied}, 429)
    ans = assistant.ask(conn(), body.question, assistant.GroqChat(key))
    return JSONResponse(ans.as_dict())
