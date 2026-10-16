"""MyStatus Android receiver API plus a token-protected device dashboard."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

APP_VERSION = "1.1.0"
DB_PATH = os.environ.get("STATUS_DB_PATH", "/data/status.db")
API_TOKEN = os.environ.get("STATUS_API_TOKEN", "").strip()
ADMIN_TOKEN = os.environ.get("STATUS_ADMIN_TOKEN", "").strip()
MAX_BODY_BYTES = 64 * 1024
ALLOWED_EVENT_TYPES = {"location", "activity", "music"}
ADMIN_SESSION_COOKIE = "maistatus_admin_session"
ADMIN_SESSION_TTL_SECONDS = 8 * 60 * 60
DEVICE_ONLINE_WINDOW_MS = 45 * 60 * 1000

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
                received_at INTEGER NOT NULL,
                device_id TEXT
            )
            """
        )
        # Safe migration for databases created by versions before device tracking.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
        if "device_id" not in columns:
            conn.execute("ALTER TABLE events ADD COLUMN device_id TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY NOT NULL,
                device_name TEXT NOT NULL,
                platform TEXT NOT NULL,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, event_time DESC)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_device_time ON events(device_id, received_at DESC)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_last_seen ON devices(last_seen DESC)")


@app.on_event("startup")
def startup() -> None:
    if len(API_TOKEN) < 32:
        raise RuntimeError(
            "STATUS_API_TOKEN must be set to a random secret of at least 32 characters. "
            "Run ./setup.sh to generate one."
        )
    if len(ADMIN_TOKEN) < 32:
        raise RuntimeError(
            "STATUS_ADMIN_TOKEN must be set to a separate random secret of at least 32 characters. "
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
        raise HTTPException(
            status_code=422,
            detail="event_id must be a non-empty string of at most 128 characters",
        )
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
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Payload contains unsupported JSON values") from None
    return event_id, event_type, event_time, payload_json


def _device_metadata(payload: Any) -> tuple[str | None, str, str]:
    """Return optional stable ID and safe display metadata; old clients remain valid."""
    device_id = payload.get("device_id")
    if device_id is None:
        return None, "未识别的旧版客户端", "unknown"
    if not isinstance(device_id, str) or not device_id.strip() or len(device_id) > 128:
        raise HTTPException(status_code=422, detail="device_id must be a non-empty string of at most 128 characters")
    device_id = device_id.strip()

    device_name = payload.get("device_name", "Android device")
    if not isinstance(device_name, str) or len(device_name.strip()) > 80:
        raise HTTPException(status_code=422, detail="device_name must be a string of at most 80 characters")
    device_name = device_name.strip() or "Android device"

    platform = payload.get("platform", "android")
    if not isinstance(platform, str) or not platform.strip() or len(platform.strip()) > 32:
        raise HTTPException(status_code=422, detail="platform must be a non-empty string of at most 32 characters")
    return device_id, device_name, platform.strip().lower()


def _upsert_device(
    conn: sqlite3.Connection,
    device_id: str,
    device_name: str,
    platform: str,
    now: int,
) -> None:
    conn.execute(
        """
        INSERT INTO devices(device_id, device_name, platform, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            device_name = excluded.device_name,
            platform = excluded.platform,
            last_seen = MAX(devices.last_seen, excluded.last_seen)
        """,
        (device_id, device_name, platform, now, now),
    )


def _event_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "event_time": row["event_time"],
        "event_type": row["event_type"],
        "received_at": row["received_at"],
        "data": json.loads(row["payload_json"]),
    }


def _is_https_request(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def _valid_admin_session(value: str | None) -> bool:
    if not value:
        return False
    try:
        expires_text, supplied_signature = value.split(".", 1)
        expires = int(expires_text)
    except (ValueError, TypeError):
        return False
    now = int(time.time())
    if expires < now or expires > now + ADMIN_SESSION_TTL_SECONDS + 60:
        return False
    expected_signature = hmac.new(
        ADMIN_TOKEN.encode("utf-8"),
        ("maistatus-admin-session:" + expires_text).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def require_admin(request: Request) -> None:
    """Admin page/API must use HTTPS plus the separate token or its signed session cookie."""
    if not _is_https_request(request):
        raise HTTPException(status_code=400, detail="HTTPS is required for admin access")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        if hmac.compare_digest(supplied, ADMIN_TOKEN):
            return
    if _valid_admin_session(request.cookies.get(ADMIN_SESSION_COOKIE)):
        return
    raise HTTPException(status_code=401, detail="Admin authentication required")


_ADMIN_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>MyStatus · 设备管理</title>
<style>
:root{color-scheme:light;--bg:#f5f7fb;--panel:#fff;--ink:#172033;--muted:#69758b;--line:#e4e9f2;--accent:#4f46e5;--good:#087f5b;--goodbg:#d9fbe9;--bad:#9a5700;--badbg:#fff0c2;--shadow:0 18px 48px rgba(31,45,85,.08)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink)}
main{max-width:1100px;margin:0 auto;padding:32px 20px 48px}.top{display:flex;align-items:center;gap:14px;margin-bottom:28px}
.logo{width:48px;height:48px;display:grid;place-items:center;border-radius:15px;background:#e8eaff;color:var(--accent);font-size:25px}
h1{font-size:24px;line-height:1.2;margin:0 0 5px}.subtitle,.muted{color:var(--muted)}.subtitle{font-size:13px}.pill{margin-left:auto;padding:6px 10px;border:1px solid #bbf7d0;border-radius:999px;background:#effdf5;color:var(--good);font-size:12px;font-weight:700}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:18px;box-shadow:var(--shadow);padding:22px;margin-bottom:18px}
.login{max-width:460px;margin:56px auto}.login p{color:var(--muted)}label{display:block;font-weight:650;margin:18px 0 7px}
input{width:100%;border:1px solid #cbd4e4;border-radius:11px;padding:12px 13px;font:inherit;outline:none}input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #4f46e51c}
button{font:inherit;font-weight:650;border:0;border-radius:11px;background:var(--accent);color:white;padding:10px 15px;cursor:pointer}button:disabled{opacity:.55;cursor:not-allowed}.secondary{background:white;color:var(--ink);border:1px solid var(--line)}.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.error{color:#b42318;min-height:24px;margin:12px 0 0;font-size:13px}.hint{font-size:12px;color:var(--muted)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:18px}.stat{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:17px 18px;min-width:0}.stat span{display:block;color:var(--muted);font-size:13px}.stat strong{display:block;font-size:29px;letter-spacing:-.7px;margin-top:5px}
.bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.bar h2{font-size:18px;margin:0 auto 0 0}.bar .muted{font-size:12px}
.table-wrap{overflow-x:auto;margin-top:14px}table{width:100%;border-collapse:collapse;min-width:620px}th,td{text-align:left;padding:14px 10px;border-bottom:1px solid var(--line)}th{font-size:12px;color:var(--muted);font-weight:650}tbody tr:last-child td{border-bottom:0}
.device{display:flex;align-items:center;gap:12px;min-width:190px}.device-icon{width:38px;height:38px;border-radius:12px;background:#eff1ff;display:grid;place-items:center;color:var(--accent);font-size:19px}.device-name{font-weight:700}.device-id{font:12px ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);overflow-wrap:anywhere}
.status{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:750}.online{color:var(--good);background:var(--goodbg)}.offline{color:var(--bad);background:var(--badbg)}.empty{text-align:center;color:var(--muted);padding:34px 12px}
.notice{border:1px solid #f3d99b;background:#fff9e8;color:#78520c;border-radius:13px;padding:12px 14px;font-size:13px;margin:14px 0}
#dashboard[hidden],#login[hidden],#httpsWarning[hidden]{display:none!important}
@media(max-width:700px){main{padding:22px 13px 32px}.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat strong{font-size:25px}.panel{padding:16px}.top{margin-bottom:20px}h1{font-size:21px}}
</style>
</head>
<body>
<main>
<header class="top"><div class="logo" aria-hidden="true">▦</div><div><h1>MyStatus 设备管理</h1><div class="subtitle">Device Console · 设备连接与上报概览</div></div><span class="pill">HTTPS · TOKEN AUTH</span></header>
<section id="httpsWarning" class="panel" hidden><h2>请使用 HTTPS 访问</h2><p>管理令牌不会通过 HTTP 提交。请改用配置了有效 TLS 证书的 HTTPS 域名打开本页面。</p></section>
<section id="login" class="panel login" hidden>
<h2>管理员验证</h2><p>输入服务器生成的管理令牌。令牌与 Android 设备写入令牌相互独立。</p>
<form id="loginForm"><label for="token">管理令牌</label><input id="token" name="token" type="password" autocomplete="current-password" required maxlength="512" placeholder="粘贴 STATUS_ADMIN_TOKEN"><div class="actions" style="margin-top:16px"><button id="loginButton" type="submit">验证并进入</button></div><div id="loginError" class="error" role="status"></div></form>
<div class="hint">仅在当前浏览器建立 8 小时的安全会话；令牌不会保存在网页存储或 URL 中。</div>
</section>
<section id="dashboard" hidden>
<div class="stats">
<div class="stat"><span>已登记设备</span><strong id="totalDevices">—</strong></div>
<div class="stat"><span>最近活跃</span><strong id="onlineDevices">—</strong></div>
<div class="stat"><span>近 24 小时事件</span><strong id="recentEvents">—</strong></div>
<div class="stat"><span>旧客户端未识别事件</span><strong id="legacyEvents">—</strong></div>
</div>
<div class="panel">
<div class="bar"><h2>设备列表</h2><span id="updatedAt" class="muted">等待加载…</span><button id="refreshButton" class="secondary" type="button">刷新</button><button id="logoutButton" class="secondary" type="button">退出登录</button></div>
<div class="notice">“最近活跃”表示过去 45 分钟内收到过设备心跳或状态上报，不代表维持着实时 TCP 连接。旧版客户端没有稳定设备 ID，会单独统计而不会冒充独立设备。</div>
<div class="table-wrap"><table><thead><tr><th>设备</th><th>平台</th><th>状态</th><th>最近通信</th><th>已接收事件</th></tr></thead><tbody id="deviceRows"></tbody></table></div>
<div id="emptyState" class="empty" hidden>暂时没有已登记设备。安装新版 MyStatus 后，设备将在首次心跳或上报时显示。</div>
<div id="dashboardError" class="error" role="status"></div>
</div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
let refreshTimer=null;
function show(which){$("httpsWarning").hidden=which!=="https";$("login").hidden=which!=="login";$("dashboard").hidden=which!=="dashboard";}
function esc(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function dateText(value){if(!value)return "暂无";return new Date(value).toLocaleString("zh-CN",{hour12:false});}
function ago(value,now){if(!value)return "暂无";const seconds=Math.max(0,Math.floor((now-value)/1000));if(seconds<60)return "刚刚";if(seconds<3600)return Math.floor(seconds/60)+" 分钟前";if(seconds<86400)return Math.floor(seconds/3600)+" 小时前";return Math.floor(seconds/86400)+" 天前";}
async function fetchDevices(){
  const response=await fetch("./api/devices",{method:"GET",credentials:"same-origin",cache:"no-store",headers:{"Accept":"application/json"}});
  if(response.status===401){if(refreshTimer)clearInterval(refreshTimer);show("login");return null;}
  if(!response.ok)throw new Error("读取设备列表失败（HTTP "+response.status+"）");
  return await response.json();
}
function render(data){
  const s=data.summary||{},now=data.server_time||Date.now();
  $("totalDevices").textContent=s.total_devices??0;$("onlineDevices").textContent=s.online_devices??0;
  $("recentEvents").textContent=s.events_last_24h??0;$("legacyEvents").textContent=s.legacy_events??0;
  $("updatedAt").textContent="更新于 "+dateText(now);
  const rows=data.devices||[];
  $("deviceRows").innerHTML=rows.map(d=>{
    const online=!!d.online;
    return "<tr><td><div class='device'><div class='device-icon' aria-hidden='true'>▣</div><div><div class='device-name'>"+esc(d.device_name)+"</div><div class='device-id'>"+esc(d.device_id)+"</div></div></div></td><td>"+esc(d.platform)+"</td><td><span class='status "+(online?"online":"offline")+"'>"+(online?"● 最近活跃":"● 离线")+"</span></td><td title='"+esc(dateText(d.last_seen))+"'>"+esc(ago(d.last_seen,now))+"</td><td>"+esc(d.event_count)+"</td></tr>";
  }).join("");
  $("emptyState").hidden=rows.length!==0;
  $("dashboardError").textContent="";
}
async function refresh(){
  $("refreshButton").disabled=true;
  try{const data=await fetchDevices();if(data){show("dashboard");render(data);}}
  catch(error){$("dashboardError").textContent=error.message||"网络请求失败，请检查 HTTPS 代理与服务端。";}
  finally{$("refreshButton").disabled=false;}
}
$("loginForm").addEventListener("submit",async event=>{
  event.preventDefault();$("loginButton").disabled=true;$("loginError").textContent="";
  try{
    const response=await fetch("./login",{method:"POST",credentials:"same-origin",cache:"no-store",headers:{"Content-Type":"application/json","Accept":"application/json"},body:JSON.stringify({token:$("token").value})});
    const data=await response.json().catch(()=>({}));
    if(!response.ok)throw new Error(data.detail||"验证失败，请检查管理令牌。");
    $("token").value="";show("dashboard");await refresh();
    if(!refreshTimer)refreshTimer=setInterval(refresh,30000);
  }catch(error){$("loginError").textContent=error.message||"验证失败。";}
  finally{$("loginButton").disabled=false;}
});
$("refreshButton").addEventListener("click",refresh);
$("logoutButton").addEventListener("click",async()=>{
  try{await fetch("./logout",{method:"POST",credentials:"same-origin",cache:"no-store"});}catch(_){}
  if(refreshTimer)clearInterval(refreshTimer);refreshTimer=null;show("login");
});
if(location.protocol!=="https:"){show("https");}
else{refresh().then(()=>{if(!$("dashboard").hidden&&!refreshTimer)refreshTimer=setInterval(refresh,30000);});}
</script>
</body></html>"""


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Minimal liveness endpoint; never returns personal status data."""
    return {"ok": True, "service": "maistatus-receiver", "version": APP_VERSION}


@app.post("/api/v1/events")
@app.post("/api/v1/evens", include_in_schema=False)  # Backward compatibility with older Android builds.
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
    device_id, device_name, platform = _device_metadata(payload)
    received_at = _now_ms()

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if device_id:
            _upsert_device(conn, device_id, device_name, platform, received_at)

        existing = conn.execute(
            "SELECT event_id FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if existing:
            return {"ok": True, "duplicate": True, "event_id": event_id}

        key_owner = conn.execute(
            "SELECT event_id FROM events WHERE idempotency_key = ?",
            (idempotency_key or event_id,),
        ).fetchone()
        if key_owner:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key already belongs to another event",
            )

        conn.execute(
            """
            INSERT INTO events(
                event_id, idempotency_key, event_time, event_type, payload_json, received_at, device_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                idempotency_key or event_id,
                event_time,
                event_type,
                payload_json,
                received_at,
                device_id,
            ),
        )

    return {"ok": True, "duplicate": False, "event_id": event_id, "received_at": received_at}


@app.post("/api/v1/devices/heartbeat")
async def device_heartbeat(
    request: Request,
    _authorized: None = Depends(require_token),
) -> dict[str, Any]:
    """Record a lightweight authenticated heartbeat so idle devices can still appear active."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > 8 * 1024:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Valid JSON required") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    device_id, device_name, platform = _device_metadata(payload)
    if not device_id:
        raise HTTPException(status_code=422, detail="device_id is required for heartbeat")
    now = _now_ms()
    with _connect() as conn:
        _upsert_device(conn, device_id, device_name, platform, now)
    return {"ok": True, "device_id": device_id, "last_seen": now}


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
                result[event_type] = {
                    **event["data"],
                    "event_id": event["event_id"],
                    "event_time": event["event_time"],
                    "received_at": event["received_at"],
                }
    timestamps = [
        result[k]["event_time"]
        for k in ("location", "activity", "music")
        if result[k]
    ]
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


@app.get("/admin/devices", response_class=HTMLResponse)
def admin_devices_page() -> HTMLResponse:
    """Serve the public shell; all device data requires an authenticated admin session."""
    return HTMLResponse(
        _ADMIN_HTML,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.post("/admin/login")
async def admin_login(request: Request) -> JSONResponse:
    if not _is_https_request(request):
        raise HTTPException(status_code=400, detail="HTTPS is required for admin login")
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Valid JSON required") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="JSON object required")
    supplied = payload.get("token")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    expires = int(time.time()) + ADMIN_SESSION_TTL_SECONDS
    signature = hmac.new(
        ADMIN_TOKEN.encode("utf-8"),
        ("maistatus-admin-session:" + str(expires)).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    response = JSONResponse({"ok": True, "expires_at": expires * 1000})
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=ADMIN_SESSION_COOKIE,
        value=f"{expires}.{signature}",
        max_age=ADMIN_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/admin/logout")
def admin_logout(
    request: Request,
    _authorized: None = Depends(require_admin),
) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.headers["Cache-Control"] = "no-store"
    response.delete_cookie(
        key=ADMIN_SESSION_COOKIE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.get("/admin/api/devices", dependencies=[Depends(require_admin)])
def admin_list_devices() -> dict[str, Any]:
    """Return minimal device metadata and aggregate counts; never return event payloads."""
    now = _now_ms()
    cutoff = now - DEVICE_ONLINE_WINDOW_MS
    recent_cutoff = now - 24 * 60 * 60 * 1000
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.device_name, d.platform, d.first_seen, d.last_seen,
                   (SELECT COUNT(*) FROM events e WHERE e.device_id = d.device_id) AS event_count
            FROM devices d
            ORDER BY d.last_seen DESC
            """
        ).fetchall()
        legacy = conn.execute(
            """
            SELECT COUNT(*) AS event_count, MAX(received_at) AS last_seen
            FROM events WHERE device_id IS NULL
            """
        ).fetchone()
        recent_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE received_at >= ?", (recent_cutoff,)
        ).fetchone()[0]

    devices = [
        {
            "device_id": row["device_id"],
            "device_name": row["device_name"],
            "platform": row["platform"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "event_count": row["event_count"],
            "online": row["last_seen"] >= cutoff,
        }
        for row in rows
    ]
    online_count = sum(1 for device in devices if device["online"])
    return {
        "ok": True,
        "server_time": now,
        "online_window_ms": DEVICE_ONLINE_WINDOW_MS,
        "summary": {
            "total_devices": len(devices),
            "online_devices": online_count,
            "events_last_24h": recent_events,
            "legacy_events": legacy["event_count"],
            "legacy_last_seen": legacy["last_seen"],
        },
        "devices": devices,
    }
