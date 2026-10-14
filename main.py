"""MyStatus Android receiver API.

Implements the upload contract used by XieBrok/Maistatus-android:
- POST JSON with Authorization: Bearer <token>
- Idempotency-Key equals event_id
- event_time is Unix epoch milliseconds
- event_type is location, activity, or music

Sensitive event payloads and credentials are deliberately not logged.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

APP_VERSION = "1.0.0"
DB_PATH = os.environ.get("STATUS_DB_PATH", "/data/status.db")
API_TOKEN = os.environ.get("STATUS_API_TOKEN", "").strip()
MAX_BODY_BYTES = 64 * 1024
ALLOWED_EVENT_TYPES = {"location", "activity", "music"}

app = FastAPI(
    title="MyStatus Receiver API",
    version=APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
_bearer = HTTPBearer(auto_error=False)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _init_db() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                event_time INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('location', 'activity', 'music')),
                payload_json TEXT NOT NULL,
                received_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, event_time DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time DESC)"
        )


@app.on_event("startup")
def startup() -> None:
    if len(API_TOKEN) < 32:
        raise RuntimeError(
            "STATUS_API_TOKEN must be set to a random secret of at least 32 characters. "
            "Run ./setup.sh to generate one."
        )
    _init_db()


def require_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(credentials.credentials, API_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _validate_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HTTPException(status_code=422, detail=f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise HTTPException(status_code=422, detail=f"{field} must be finite")
    return result


def _validate_event(payload: Any, idempotency_key: str | None) -> tuple[str, str, int, str]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON object required")

    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 128:
        raise HTTPException(status_code=422, detail="event_id must be a non-empty string of at most 128 characters")
    event_id = event_id.strip()

    event_type = payload.get("event_type")
    if event_type not in ALLOWED_EVENT_TYPES:
        raise HTTPException(status_code=422, detail="event_type must be location, activity, or music")

    event_time = payload.get("event_time")
    if isinstance(event_time, bool) or not isinstance(event_time, int) or event_time <= 0:
        raise HTTPException(status_code=422, detail="event_time must be Unix epoch milliseconds")

    key = (idempotency_key or event_id).strip()
    if not key or len(key) > 200:
        raise HTTPException(status_code=422, detail="Invalid Idempotency-Key")
    if idempotency_key and key != event_id:
        raise HTTPException(status_code=400, detail="Idempotency-Key must match event_id")

    # Validate the fields that this Android prototype sends for each event.
    if event_type == "location":
        latitude = _validate_number(payload.get("latitude"), "latitude")
        longitude = _validate_number(payload.get("longitude"), "longitude")
        if not -90 <= latitude <= 90:
            raise HTTPException(status_code=422, detail="latitude must be between -90 and 90")
        if not -180 <= longitude <= 180:
            raise HTTPException(status_code=422, detail="longitude must be between -180 and 180")
        if "accuracy_meters" in payload:
            accuracy = _validate_number(payload["accuracy_meters"], "accuracy_meters")
            if accuracy < 0:
                raise HTTPException(status_code=422, detail="accuracy_meters cannot be negative")
    elif event_type == "activity":
        activity_type = payload.get("activity_type")
        confidence = payload.get("confidence")
        if isinstance(activity_type, bool) or not isinstance(activity_type, int):
            raise HTTPException(status_code=422, detail="activity_type must be an integer")
        if isinstance(confidence, bool) or not isinstance(confidence, int) or not 0 <= confidence <= 100:
            raise HTTPException(status_code=422, detail="confidence must be an integer from 0 to 100")
    elif event_type == "music":
        state = payload.get("state")
        if state not in {"playing", "paused", "stopped"}:
            raise HTTPException(status_code=422, detail="music state must be playing, paused, or stopped")

    try:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Payload contains unsupported JSON values") from None
    return event_id, event_type, event_time, payload_json


def _event_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event_time": row["event_time"],
        "event_type": row["event_type"],
        "received_at": row["received_at"],
        "data": json.loads(row["payload_json"]),
    }


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Minimal liveness endpoint; never returns personal status data."""
    return {"ok": True, "service": "maistatus-receiver", "version": APP_VERSION}


@app.post("/api/v1/events")
async def receive_event(
    request: Request,
    _authorized: None = Depends(require_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body too large")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Valid JSON required") from None

    event_id, event_type, event_time, payload_json = _validate_event(payload, idempotency_key)
    received_at = _now_ms()

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT event_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing:
            # A repeated event is a successful upload from the client's perspective.
            return {"ok": True, "duplicate": True, "event_id": event_id}

        key_owner = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key = ?", (idempotency_key or event_id,)
        ).fetchone()
        if key_owner:
            raise HTTPException(status_code=409, detail="Idempotency-Key already belongs to another event")

        conn.execute(
            """INSERT INTO events(event_id, idempotency_key, event_time, event_type, payload_json, received_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, idempotency_key or event_id, event_time, event_type, payload_json, received_at),
        )

    return {"ok": True, "duplicate": False, "event_id": event_id, "received_at": received_at}


@app.get("/api/v1/status/latest", dependencies=[Depends(require_token)])
def latest_status() -> dict[str, Any]:
    """Return the latest known event for each status category, for a future MaiBot connector."""
    result: dict[str, Any] = {"ok": True, "location": None, "activity": None, "music": None}
    with _connect() as conn:
        for event_type in ("location", "activity", "music"):
            row = conn.execute(
                """SELECT event_id, event_time, event_type, payload_json, received_at
                   FROM events WHERE event_type = ?
                   ORDER BY event_time DESC, received_at DESC LIMIT 1""",
                (event_type,),
            ).fetchone()
            if row:
                event = _event_to_dict(row)
                result[event_type] = {**event["data"], "event_id": event["event_id"],
                                      "event_time": event["event_time"], "received_at": event["received_at"]}
    timestamps = [result[k]["event_time"] for k in ("location", "activity", "music") if result[k]]
    result["last_event_time"] = max(timestamps) if timestamps else None
    return result


@app.get("/api/v1/events", dependencies=[Depends(require_token)])
def list_events(
    limit: int = Query(default=50, ge=1, le=200),
    event_type: Literal["location", "activity", "music"] | None = None,
) -> dict[str, Any]:
    """List recent events for authenticated debugging/integration."""
    with _connect() as conn:
        if event_type:
            rows = conn.execute(
                """SELECT event_id, event_time, event_type, payload_json, received_at
                   FROM events WHERE event_type = ?
                   ORDER BY event_time DESC, received_at DESC LIMIT ?""",
                (event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT event_id, event_time, event_type, payload_json, received_at
                   FROM events ORDER BY event_time DESC, received_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
    return {"ok": True, "count": len(rows), "events": [_event_to_dict(row) for row in rows]}
