"""原型易用性測試 獨立網站版

環境變數：
  ADMIN_PASSWORD   管理者密碼（必要）
  SECRET_KEY       選填，用來簽署登入狀態的隨機字串
  AI_PROVIDER / AI_API_KEY / AI_MODEL / AI_BASE_URL   見 ai.py
  DATABASE_URL     選填，PostgreSQL 連線字串（Vercel 加入 Neon 後會自動設定）；
                   沒設定時使用 SQLite 檔案
  DATA_DIR         SQLite 存放位置，預設 ./data
  PORT             預設 8080
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from functools import wraps
from io import BytesIO
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import quote
from urllib.parse import urlsplit
from urllib.parse import urlencode, urlunsplit, parse_qsl

from flask import Flask, Response, abort, g, jsonify, request, send_from_directory, session

import ai

ROOT = Path(__file__).parent
ON_VERCEL = bool(os.environ.get("VERCEL"))
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/tmp/ut-data" if ON_VERCEL else ROOT / "data"))
DB_PATH = DATA_DIR / "app.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_HTML = 4 * 1024 * 1024          # Vercel 單次請求上限約 4.5 MB
MAX_ZIP_EXPANDED = 16 * 1024 * 1024
MAX_ZIP_FILES = 250
MAX_SESSION = 512 * 1024

app = Flask(__name__, static_folder=str(ROOT / "static"))
# 沒設定 SECRET_KEY 時由管理密碼推導，確保多台伺服器之間登入狀態一致
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(("ut-session:" + ADMIN_PASSWORD).encode()).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=ON_VERCEL or os.environ.get("COOKIE_SECURE", "0") == "1",
                  MAX_CONTENT_LENGTH=MAX_HTML + 256 * 1024)


# ---------- 資料庫 ----------

class _PG:
    """讓 PostgreSQL 與 SQLite 用同一套寫法（? 參數）。"""
    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row
        self.conn = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def _connect():
    if USE_PG:
        return _PG(DATABASE_URL)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def db():
    if "db" not in g:
        g.db = _connect()
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


SCHEMA = [
    "CREATE TABLE IF NOT EXISTS studies(id TEXT PRIMARY KEY, name TEXT, goal TEXT, html TEXT, tasks TEXT DEFAULT '[]', created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, study_id TEXT, role TEXT, data TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS study_assets(study_id TEXT, path TEXT, mime TEXT, data TEXT, PRIMARY KEY(study_id, path))",
    "CREATE TABLE IF NOT EXISTS study_sources(study_id TEXT PRIMARY KEY, url TEXT)",
    "CREATE TABLE IF NOT EXISTS study_integrations(study_id TEXT PRIMARY KEY, mode TEXT, origin TEXT, last_seen TEXT)",
    "CREATE TABLE IF NOT EXISTS instrument_runs(id TEXT PRIMARY KEY, study_id TEXT, kind TEXT, role TEXT, active_task TEXT, started_at TEXT, finished_at TEXT, last_seen TEXT, success INTEGER DEFAULT 0, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS instrument_events(id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, kind TEXT, data TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS screen_images(study_id TEXT, task_id TEXT, screen TEXT, device TEXT, mime TEXT, data TEXT, PRIMARY KEY(study_id,task_id,screen,device))",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_study ON sessions(study_id)",
    "CREATE INDEX IF NOT EXISTS idx_instrument_events_run ON instrument_events(run_id,task_id)",
]


def init_db():
    conn = _connect()
    try:
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix):
    return prefix + secrets.token_urlsafe(9).replace("-", "a").replace("_", "b")


PASSWORD_ITERATIONS = 310_000


def get_setting(key, default=""):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    db().execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def make_password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def password_matches(password):
    # 環境變數 ADMIN_PASSWORD 一律視為有效的「主密碼」：即使資料庫裡另外存了
    # 以站內「更改密碼」設定的雜湊，也能用它登入。這樣忘記站內密碼時，
    # 只要改 Vercel 的 ADMIN_PASSWORD 環境變數並重新部署，就能重新進入。
    if ADMIN_PASSWORD and hmac.compare_digest(password, ADMIN_PASSWORD):
        return True
    stored = get_setting("admin_password_hash")
    if not stored:
        return False
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def password_version():
    return get_setting("admin_password_version", "0")


def is_admin():
    return bool(session.get("admin")) and session.get("password_version", "0") == password_version()


def get_study(sid):
    row = db().execute("SELECT * FROM studies WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404)
    return row


def study_json(row, with_html=False):
    integration = study_integration(row["id"])
    images = db().execute("SELECT task_id,screen,device FROM screen_images WHERE study_id=?", (row["id"],)).fetchall()
    d = {"id": row["id"], "name": row["name"], "goal": row["goal"], "createdAt": row["created_at"],
         "tasks": json.loads(row["tasks"] or "[]"), "size": len(row["html"] or ""),
         "sourceURL": study_source(row["id"]), "integration": integration,
         "screenshots": [dict(x) for x in images]}
    if with_html:
        d["html"] = row["html"]
    return d


def study_source(sid):
    row = db().execute("SELECT url FROM study_sources WHERE study_id=?", (sid,)).fetchone()
    return row["url"] if row else ""


def study_integration(sid):
    row = db().execute("SELECT mode,origin,last_seen FROM study_integrations WHERE study_id=?", (sid,)).fetchone()
    return dict(row) if row else None


def target_url(url, run_id):
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != "ut_run"]
    query.append(("ut_run", run_id))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def run_row(run_id):
    row = db().execute("SELECT * FROM instrument_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        abort(404)
    if time.time() - datetime.fromisoformat(row["created_at"]).timestamp() > 24 * 3600:
        abort(404)
    return row


def origin_allowed(run):
    integration = study_integration(run["study_id"])
    origin = request.headers.get("Origin", "")
    return bool(integration and origin == integration["origin"])


def instrument_cors(resp, run=None):
    if run and origin_allowed(run):
        resp.headers["Access-Control-Allow-Origin"] = request.headers["Origin"]
        resp.headers["Vary"] = "Origin"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def valid_public_url(value):
    try:
        parsed = urlsplit(str(value).strip())
        host = parsed.hostname
        parsed.port  # Force validation of a malformed explicit port.
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if host.lower() in {"localhost", "localhost.localdomain"} or host.lower().endswith((".local", ".internal", ".localhost")):
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        return True
    except (TypeError, ValueError):
        return False


def decode_html(raw):
    for encoding in ("utf-8-sig", "big5", "cp950"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace")


def clean_zip_path(name):
    """Return a safe, normalized ZIP path or None for metadata/unsafe paths."""
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    if not name or name.startswith("/") or ".." in p.parts:
        return None
    parts = [part for part in p.parts if part not in ("", ".")]
    if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
        return None
    return "/".join(parts)


def choose_zip_entry(paths):
    htmls = [p for p in paths if p.lower().endswith((".html", ".htm"))]
    if not htmls:
        return None

    def score(p):
        parts = [x.lower() for x in PurePosixPath(p).parts]
        noisy = any(x in {"node_modules", ".git", "vendor", "coverage"} for x in parts)
        built = next((i for i, x in enumerate(parts[:-1]) if x in {"dist", "build", "out", "public", "www"}), None)
        return (
            9 if noisy else
            0 if p.lower() == "index.html" else
            1 if built is not None and parts[-1] == "index.html" else
            2 if parts[-1] == "index.html" else
            3,
            built if built is not None else 99,
            len(PurePosixPath(p).parts),
            len(p),
            p.lower(),
        )

    return min(htmls, key=score)


def add_asset_base(html, sid, entry):
    directory = str(PurePosixPath(entry).parent)
    prefix = "" if directory == "." else quote(directory.strip("/"), safe="/") + "/"
    asset_endpoint = f"/api/public/study/{quote(sid, safe='')}/asset/"
    tag = f'<base href="{asset_endpoint}{prefix}">'
    # Backend templates commonly use /static/...; map those root-relative assets
    # into the uploaded package. Runtime API calls still require a real backend.
    parent = PurePosixPath(entry).parent
    root = parent.parent if parent.name.lower() in {"templates", "views"} else parent
    root_prefix = "" if str(root) == "." else quote(str(root).strip("/"), safe="/") + "/"
    asset_root = asset_endpoint + root_prefix
    html = re.sub(r'(?i)(\b(?:src|href|poster)\s*=\s*["\'])/(?!/)',
                  lambda m: m.group(1) + asset_root, html)
    html = re.sub(r'(?i)(url\(\s*["\']?)/(?!/)',
                  lambda m: m.group(1) + asset_root, html)
    lower = html.lower()
    i = lower.find("<head")
    if i >= 0:
        close = html.find(">", i)
        if close >= 0:
            return html[:close + 1] + tag + html[close + 1:]
    return tag + html


def store_asset(conn, sid, path, raw):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    conn.execute("INSERT INTO study_assets VALUES(?,?,?,?)",
                 (sid, path, mime, base64.b64encode(raw).decode("ascii")))


def save_packaged_study(sid, name, goal, files):
    entry = choose_zip_entry(files)
    if not entry:
        raise ValueError("資料夾內找不到 .html 或 .htm 首頁")
    html = decode_html(files[entry])
    if not html.strip():
        raise ValueError("找到的入口 HTML 是空白檔案")
    html = add_asset_base(html, sid, entry)
    conn = db()
    conn.execute("INSERT INTO studies VALUES(?,?,?,?,?,?)",
                 (sid, name, goal, html, "[]", now()))
    for path, data in files.items():
        store_asset(conn, sid, path, data)
    conn.commit()
    return entry


# ---------- 登入 ----------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not is_admin():
            session.clear()
            return jsonify(error="請先登入"), 401
        return fn(*a, **kw)
    return wrapper


_fails = {}


@app.post("/api/login")
def login():
    ip = request.remote_addr or ""
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 600]
    if len(recent) >= 8:
        return jsonify(error="嘗試次數過多，請 10 分鐘後再試"), 429
    pw = (request.get_json(silent=True) or {}).get("password", "")
    configured = bool(get_setting("admin_password_hash") or ADMIN_PASSWORD)
    if not configured or not password_matches(pw):
        _fails[ip] = recent + [time.time()]
        return jsonify(error="密碼錯誤" if configured else "伺服器尚未設定 ADMIN_PASSWORD"), 401
    session["admin"] = True
    session["password_version"] = password_version()
    session.permanent = True
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.post("/api/admin/password")
@admin_required
def change_password():
    body = request.get_json(silent=True) or {}
    current = str(body.get("current", ""))
    new = str(body.get("new", ""))
    if not password_matches(current):
        return jsonify(error="目前密碼不正確"), 400
    if len(new) < 10:
        return jsonify(error="新密碼至少需要 10 個字元"), 400
    if len(new) > 200:
        return jsonify(error="新密碼過長"), 400
    if hmac.compare_digest(current, new):
        return jsonify(error="新密碼不可與目前密碼相同"), 400
    version = secrets.token_urlsafe(12)
    set_setting("admin_password_hash", make_password_hash(new))
    set_setting("admin_password_version", version)
    db().commit()
    session["password_version"] = version
    return jsonify(ok=True)


# ---------- 頁面 ----------

@app.get("/")
@app.get("/t/<sid>")
def page(sid=None):
    return send_from_directory(app.static_folder, "index.html")


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    return resp


# ---------- 受測者（公開） ----------

@app.get("/api/public/study/<sid>")
def public_study(sid):
    row = get_study(sid)
    tasks = [{"id": t["id"], "prompt": t["prompt"], "cond": t["cond"]} for t in json.loads(row["tasks"] or "[]")]
    integration = study_integration(sid)
    return jsonify(name=row["name"], tasks=tasks, html=row["html"], url=study_source(sid),
                   mode=integration["mode"] if integration else "legacy")


@app.get("/api/public/study/<sid>/asset/<path:asset_path>")
def public_asset(sid, asset_path):
    get_study(sid)
    path = clean_zip_path(asset_path)
    if not path or path != asset_path:
        abort(404)
    row = db().execute("SELECT mime,data FROM study_assets WHERE study_id=? AND path=?",
                       (sid, path)).fetchone()
    if not row:
        abort(404)
    try:
        raw = base64.b64decode(row["data"], validate=True)
    except (ValueError, TypeError):
        abort(500)
    resp = Response(raw, mimetype=row["mime"] or "application/octet-stream")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.post("/api/public/study/<sid>/session")
def public_session(sid):
    get_study(sid)
    raw = request.get_data(cache=False)
    if len(raw) > MAX_SESSION:
        return jsonify(error="資料太大"), 413
    try:
        data = json.loads(raw)
        assert isinstance(data.get("results"), list)
    except (ValueError, AssertionError, AttributeError):
        return jsonify(error="格式錯誤"), 400
    run_id = str(data.get("runId") or "")
    if run_id:
        run = run_row(run_id)
        if run["study_id"] != sid or run["kind"] != "test" or run["active_task"] == "__done__":
            return jsonify(error="測試場次無效"), 400
    data = {"role": str(data.get("role", ""))[:20], "onsite": bool(data.get("onsite")),
            "device": data.get("device") if isinstance(data.get("device"), dict) else {},
            "results": data["results"][:20]}
    sid_ = new_id("x")
    db().execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (sid_, sid, data["role"], json.dumps(data, ensure_ascii=False), now()))
    if run_id:
        db().execute("UPDATE instrument_runs SET active_task='__done__',finished_at=? WHERE id=?", (now(), run_id))
    db().commit()
    return jsonify(ok=True, id=sid_)


@app.post("/api/public/study/<sid>/run")
def create_test_run(sid):
    get_study(sid)
    if not study_integration(sid):
        abort(404)
    body = request.get_json(silent=True) or {}
    run_id = new_id("r") + secrets.token_urlsafe(12)
    role = str(body.get("role") or "")[:20]
    db().execute("INSERT INTO instrument_runs(id,study_id,kind,role,created_at) VALUES(?,?,?,?,?)",
                 (run_id, sid, "test", role, now()))
    db().commit()
    return jsonify(run=run_id, url=target_url(study_source(sid), run_id))


@app.post("/api/public/run/<run_id>/task")
def run_task(run_id):
    run = run_row(run_id)
    if run["kind"] != "test" or run["active_task"] == "__done__":
        abort(404)
    body = request.get_json(silent=True) or {}
    task_id = str(body.get("taskId") or "")
    tasks = json.loads(get_study(run["study_id"])["tasks"] or "[]")
    if task_id not in [t["id"] for t in tasks]:
        return jsonify(error="找不到任務"), 400
    db().execute("UPDATE instrument_runs SET active_task=?,started_at=?,finished_at=NULL,success=0 WHERE id=?",
                 (task_id, str(time.time()), run_id))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/public/run/<run_id>/status")
def run_status(run_id):
    run = run_row(run_id)
    return jsonify(ready=bool(run["last_seen"]), activeTask=run["active_task"],
                   success=bool(run["success"]), lastSeen=run["last_seen"])


@app.post("/api/public/run/<run_id>/finish")
def finish_run_task(run_id):
    run = run_row(run_id)
    task_id = run["active_task"]
    if run["kind"] != "test" or not task_id or run["finished_at"]:
        return jsonify(error="沒有進行中的任務"), 400
    rows = db().execute("SELECT kind,data FROM instrument_events WHERE run_id=? AND task_id=? ORDER BY created_at,id",
                        (run_id, task_id)).fetchall()
    clicks = [json.loads(r["data"]) for r in rows if r["kind"] == "click"][:200]
    active_ms = sum(min(10000, max(0, int(json.loads(r["data"]).get("ms", 0)))) for r in rows if r["kind"] == "active")
    elapsed = max(0, int((time.time() - float(run["started_at"])) * 1000))
    success = bool(run["success"]) and not bool((request.get_json(silent=True) or {}).get("giveUp"))
    db().execute("UPDATE instrument_runs SET finished_at=?,active_task=NULL WHERE id=?", (now(), run_id))
    db().commit()
    return jsonify(taskId=task_id, success=success, timeMs=elapsed, activeMs=min(active_ms, elapsed), clicks=clicks)


@app.route("/api/instrument/<run_id>", methods=["GET", "POST", "OPTIONS"])
def instrument(run_id):
    run = run_row(run_id)
    if not origin_allowed(run):
        return jsonify(error="不允許的網站來源"), 403
    if request.method == "OPTIONS":
        return instrument_cors(Response(status=204), run)
    if request.method == "GET":
        if run["active_task"] == "__done__":
            return instrument_cors(jsonify(activeTask=None, kind=run["kind"], success=False, cond=None, ended=True), run)
        condition = None
        if run["active_task"] and run["kind"] == "test":
            tasks = json.loads(get_study(run["study_id"])["tasks"] or "[]")
            condition = next((t.get("cond") for t in tasks if t["id"] == run["active_task"]), None)
        return instrument_cors(jsonify(activeTask=run["active_task"], kind=run["kind"],
                                       success=bool(run["success"]), cond=condition, ended=False), run)
    body = request.get_json(silent=True) or {}
    events = body.get("events")
    if not isinstance(events, list) or len(events) > 30 or request.content_length and request.content_length > 16000:
        return instrument_cors(jsonify(error="事件格式錯誤"), run), 400
    existing = db().execute("SELECT COUNT(*) AS n FROM instrument_events WHERE run_id=?", (run_id,)).fetchone()["n"]
    if existing >= 1000:
        return instrument_cors(jsonify(error="事件數量已達上限"), run), 429
    task_id = run["active_task"]
    for event in events:
        if not isinstance(event, dict) or event.get("type") not in {"ready", "click", "active", "complete"}:
            continue
        kind = event["type"]
        if kind != "ready" and (not task_id or run["finished_at"]):
            continue
        if kind == "complete" and run["kind"] == "test":
            db().execute("UPDATE instrument_runs SET success=1 WHERE id=?", (run_id,))
        if kind in {"click", "active"}:
            value = event.get("data") if isinstance(event.get("data"), dict) else {}
            if kind == "click":
                try:
                    x, y = float(value.get("x") or 0), float(value.get("y") or 0)
                    w, h = int(value.get("w") or 0), int(value.get("h") or 0)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not math.isfinite(x) or not math.isfinite(y):
                    continue
                clean = {"screen": str(value.get("screen") or "")[:80], "element": str(value.get("element") or "")[:100],
                         "x": min(1, max(0, x)), "y": min(1, max(0, y)),
                         "w": min(4000, max(0, w)), "h": min(4000, max(0, h)),
                         "device": "mobile" if w < 600 else "desktop", "interactive": bool(value.get("interactive"))}
            else:
                try:
                    clean = {"ms": min(10000, max(0, int(value.get("ms") or 0)))}
                except (TypeError, ValueError, OverflowError):
                    continue
            event_id = str(event.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", event_id):
                event_id = new_id("e")
            db().execute("INSERT INTO instrument_events VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
                         (run_id + ":" + event_id, run_id, task_id, kind, json.dumps(clean),
                          datetime.now(timezone.utc).isoformat(timespec="microseconds")))
    db().execute("UPDATE instrument_runs SET last_seen=? WHERE id=?", (now(), run_id))
    db().execute("UPDATE study_integrations SET last_seen=? WHERE study_id=?", (now(), run["study_id"]))
    db().commit()
    return instrument_cors(jsonify(ok=True), run)


# ---------- 管理者 ----------

@app.get("/api/me")
def me():
    return jsonify(admin=is_admin(), aiReady=ai.configured(), provider=ai.PROVIDER, model=ai.MODEL,
                   persistent=USE_PG or not ON_VERCEL)


@app.get("/api/studies")
@admin_required
def list_studies():
    rows = db().execute("SELECT s.*, (SELECT COUNT(*) FROM sessions WHERE study_id=s.id) AS n FROM studies s ORDER BY created_at DESC").fetchall()
    return jsonify([{**study_json(r), "sessions": r["n"]} for r in rows])


@app.post("/api/studies")
@admin_required
def create_study():
    body = request.get_json(silent=True) or {}
    html = str(body.get("html", ""))
    if not html.strip():
        return jsonify(error="沒有 HTML 內容"), 400
    if len(html) > MAX_HTML:
        return jsonify(error="檔案超過 4 MB"), 413
    sid = new_id("s")
    db().execute("INSERT INTO studies VALUES(?,?,?,?,?,?)",
                 (sid, str(body.get("name") or "未命名測試")[:100], str(body.get("goal") or "")[:1000], html, "[]", now()))
    db().commit()
    return jsonify(id=sid)


@app.post("/api/studies/url")
@admin_required
def create_url_study():
    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or "").strip()
    if not valid_public_url(url):
        return jsonify(error="請輸入有效的公開 HTTPS 網址（不支援內部或本機網址）"), 400
    host = (urlsplit(url).hostname or "").lower()
    if host == (request.host.split(":", 1)[0] or "").lower():
        return jsonify(error="請輸入要測試的目標網站網址"), 400
    mode = str(body.get("mode") or "web")
    if mode not in {"web", "liff"}:
        return jsonify(error="不支援的網站類型"), 400
    if mode == "liff" and (host != "liff.line.me" or not urlsplit(url).path.strip("/")):
        return jsonify(error="請填入 https://liff.line.me/ 開頭的正式 LIFF 網址"), 400
    endpoint = str(body.get("endpoint") or (url if mode == "web" else "")).strip()
    if not valid_public_url(endpoint):
        return jsonify(error="請填入受測網站的公開 HTTPS 網址"), 400
    parts = urlsplit(endpoint)
    origin = f"https://{parts.netloc.lower()}"
    sid = new_id("s")
    name = str(body.get("name") or host)[:100]
    goal = str(body.get("goal") or "")[:1000]
    conn = db()
    conn.execute("INSERT INTO studies VALUES(?,?,?,?,?,?)", (sid, name, goal, "", "[]", now()))
    conn.execute("INSERT INTO study_sources VALUES(?,?)", (sid, url))
    conn.execute("INSERT INTO study_integrations VALUES(?,?,?,NULL)", (sid, mode, origin))
    conn.commit()
    return jsonify(id=sid, url=url)


@app.post("/api/studies/<sid>/integration")
@admin_required
def enable_integration(sid):
    get_study(sid)
    url = study_source(sid)
    if not url:
        return jsonify(error="這不是網址測試"), 400
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "web")
    if mode not in {"web", "liff"} or (mode == "liff" and urlsplit(url).hostname != "liff.line.me"):
        return jsonify(error="網站類型與測試網址不符"), 400
    endpoint = str(body.get("endpoint") or (url if mode == "web" else "")).strip()
    if not valid_public_url(endpoint):
        return jsonify(error="請填入受測網站的公開 HTTPS 網址"), 400
    origin = f"https://{urlsplit(endpoint).netloc.lower()}"
    db().execute("INSERT INTO study_integrations(study_id,mode,origin) VALUES(?,?,?) ON CONFLICT(study_id) DO UPDATE SET mode=excluded.mode,origin=excluded.origin,last_seen=NULL",
                 (sid, mode, origin))
    db().commit()
    return jsonify(ok=True, mode=mode, origin=origin)


@app.post("/api/studies/import")
@admin_required
def import_study():
    folder_uploads = [item for item in request.files.getlist("files") if item.filename]
    folder_paths = request.form.getlist("paths")
    if folder_uploads:
        if len(folder_uploads) > MAX_ZIP_FILES:
            return jsonify(error=f"資料夾內檔案超過 {MAX_ZIP_FILES} 個"), 413
        files = {}
        total = 0
        for index, item in enumerate(folder_uploads):
            supplied_path = folder_paths[index] if index < len(folder_paths) else item.filename
            path = clean_zip_path(supplied_path)
            if not path:
                continue
            raw_file = item.read(MAX_HTML + 1)
            total += len(raw_file)
            if total > MAX_HTML:
                return jsonify(error="資料夾檔案合計超過 4 MB"), 413
            files[path] = raw_file
        if not files:
            return jsonify(error="選擇的資料夾沒有可上傳的檔案"), 400
        first_path = next(iter(files))
        default_name = PurePosixPath(first_path).parts[0] if "/" in first_path else "資料夾原型"
        name = str(request.form.get("name") or default_name)[:100]
        goal = str(request.form.get("goal") or "")[:1000]
        sid = new_id("s")
        try:
            entry = save_packaged_study(sid, name, goal, files)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(id=sid, entry=entry, assets=len(files), source="folder")

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="請選擇 HTML 或 ZIP 檔案"), 400
    raw = uploaded.read(MAX_HTML + 1)
    if len(raw) > MAX_HTML:
        return jsonify(error="上傳檔案超過 4 MB"), 413
    name = str(request.form.get("name") or Path(uploaded.filename).stem or "未命名測試")[:100]
    goal = str(request.form.get("goal") or "")[:1000]
    sid = new_id("s")
    filename = uploaded.filename.lower()

    if filename.endswith((".html", ".htm")):
        html = decode_html(raw)
        if not html.strip():
            return jsonify(error="沒有 HTML 內容"), 400
        if len(html.encode("utf-8")) > MAX_HTML:
            return jsonify(error="HTML 解壓後超過 4 MB"), 413
        db().execute("INSERT INTO studies VALUES(?,?,?,?,?,?)",
                     (sid, name, goal, html, "[]", now()))
        db().commit()
        return jsonify(id=sid, entry=uploaded.filename, assets=0)

    if not filename.endswith(".zip"):
        return jsonify(error="只支援 .html、.htm 或 .zip 檔案"), 400
    try:
        with zipfile.ZipFile(BytesIO(raw)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > MAX_ZIP_FILES:
                return jsonify(error=f"ZIP 內檔案超過 {MAX_ZIP_FILES} 個"), 413
            if sum(i.file_size for i in infos) > MAX_ZIP_EXPANDED:
                return jsonify(error="ZIP 解壓後超過 16 MB"), 413
            files = {}
            for info in infos:
                path = clean_zip_path(info.filename)
                if not path:
                    continue
                # Unix symlinks are not useful website assets and can be misleading.
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    continue
                files[path] = zf.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError):
        return jsonify(error="ZIP 無法讀取，請確認檔案沒有損壞或加密"), 400

    try:
        entry = save_packaged_study(sid, name, goal, files)
    except ValueError as exc:
        return jsonify(error=str(exc).replace("資料夾", "ZIP")), 400
    return jsonify(id=sid, entry=entry, assets=len(files), source="zip")


@app.get("/api/studies/<sid>")
@admin_required
def read_study(sid):
    return jsonify(study_json(get_study(sid), with_html=True))


@app.put("/api/studies/<sid>/tasks")
@admin_required
def save_tasks(sid):
    get_study(sid)
    tasks = (request.get_json(silent=True) or {}).get("tasks", [])
    clean = []
    for t in tasks[:10]:
        cond = t.get("cond") or {}
        clean.append({"id": str(t.get("id") or new_id("t"))[:40], "prompt": str(t.get("prompt", ""))[:500],
                      "cond": {"type": str(cond.get("type") or "visible")[:20], "text": str(cond.get("text", ""))[:200]},
                      "path": [str(p)[:100] for p in (t.get("path") or [])][:15],
                      "paths": [[str(p)[:100] for p in route[:20]] for route in (t.get("paths") or [])[:5] if isinstance(route, list)]})
    db().execute("UPDATE studies SET tasks=? WHERE id=?", (json.dumps(clean, ensure_ascii=False), sid))
    db().commit()
    return jsonify(tasks=clean)


@app.delete("/api/studies/<sid>")
@admin_required
def delete_study(sid):
    db().execute("DELETE FROM sessions WHERE study_id=?", (sid,))
    db().execute("DELETE FROM study_assets WHERE study_id=?", (sid,))
    db().execute("DELETE FROM study_sources WHERE study_id=?", (sid,))
    db().execute("DELETE FROM study_integrations WHERE study_id=?", (sid,))
    db().execute("DELETE FROM instrument_events WHERE run_id IN (SELECT id FROM instrument_runs WHERE study_id=?)", (sid,))
    db().execute("DELETE FROM instrument_runs WHERE study_id=?", (sid,))
    db().execute("DELETE FROM screen_images WHERE study_id=?", (sid,))
    db().execute("DELETE FROM studies WHERE id=?", (sid,))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/studies/<sid>/sessions")
@admin_required
def list_sessions(sid):
    rows = db().execute("SELECT * FROM sessions WHERE study_id=? ORDER BY created_at", (sid,)).fetchall()
    return jsonify([{**json.loads(r["data"]), "id": r["id"], "at": r["created_at"]} for r in rows])


@app.post("/api/studies/<sid>/recordings")
@admin_required
def create_recording(sid):
    study = get_study(sid)
    if not study_integration(sid):
        abort(404)
    task_id = str((request.get_json(silent=True) or {}).get("taskId") or "")
    if task_id not in [t["id"] for t in json.loads(study["tasks"] or "[]")]:
        return jsonify(error="請先儲存任務"), 400
    run_id = new_id("r") + secrets.token_urlsafe(12)
    db().execute("INSERT INTO instrument_runs(id,study_id,kind,role,active_task,started_at,created_at) VALUES(?,?,?,?,?,?,?)",
                 (run_id, sid, "record", "", task_id, str(time.time()), now()))
    db().commit()
    return jsonify(run=run_id, url=target_url(study_source(sid), run_id))


@app.post("/api/studies/<sid>/recordings/<run_id>/finish")
@admin_required
def finish_recording(sid, run_id):
    run = run_row(run_id)
    if run["study_id"] != sid or run["kind"] != "record":
        abort(404)
    rows = db().execute("SELECT data FROM instrument_events WHERE run_id=? AND kind='click' ORDER BY created_at,id", (run_id,)).fetchall()
    recorded = [json.loads(row["data"]) for row in rows]
    path = [item.get("element", "") for item in recorded]
    path = [p for i, p in enumerate(path) if p and (i == 0 or p != path[i - 1])][:20]
    db().execute("UPDATE instrument_runs SET active_task=NULL,finished_at=? WHERE id=?", (now(), run_id))
    db().commit()
    return jsonify(path=path, screens=sorted({item.get("screen", "") for item in recorded if item.get("screen")}), ready=bool(run["last_seen"]))


@app.post("/api/studies/<sid>/screenshots")
@admin_required
def upload_screen(sid):
    get_study(sid)
    task_id = request.form.get("taskId", "")[:40]
    screen = request.form.get("screen", "")[:80]
    device = request.form.get("device", "")
    tasks = json.loads(get_study(sid)["tasks"] or "[]")
    if task_id not in [t["id"] for t in tasks] or not screen or device not in {"mobile", "desktop"}:
        return jsonify(error="請選擇任務、畫面與裝置"), 400
    upload = request.files.get("file")
    raw = upload.read(2 * 1024 * 1024 + 1) if upload else b""
    if not raw or len(raw) > 2 * 1024 * 1024:
        return jsonify(error="請上傳小於 2 MB 的 PNG、JPEG 或 WebP 圖片"), 400
    mime = "image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else "image/webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else ""
    if not mime:
        return jsonify(error="只支援 PNG、JPEG 或 WebP 圖片"), 400
    db().execute("INSERT INTO screen_images VALUES(?,?,?,?,?,?) ON CONFLICT(study_id,task_id,screen,device) DO UPDATE SET mime=excluded.mime,data=excluded.data",
                 (sid, task_id, screen, device, mime, base64.b64encode(raw).decode()))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/studies/<sid>/screenshots/<task_id>/<device>")
@admin_required
def get_screen(sid, task_id, device):
    screen = request.args.get("screen", "")
    row = db().execute("SELECT mime,data FROM screen_images WHERE study_id=? AND task_id=? AND screen=? AND device=?",
                       (sid, task_id, screen, device)).fetchone()
    if not row:
        abort(404)
    resp = Response(base64.b64decode(row["data"]), mimetype=row["mime"])
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


@app.delete("/api/sessions/<xid>")
@admin_required
def delete_session(xid):
    db().execute("DELETE FROM sessions WHERE id=?", (xid,))
    db().commit()
    return jsonify(ok=True)


TASK_PROMPT = """你是資深 UX 研究員，正在為一個 HTML 原型設計「無主持人易用性測試」的任務。產品領域：台灣餐飲業的點餐與管理系統（POS、kiosk、KDS、桌邊掃碼點餐、後台報表）。
{goal}
請閱讀原型的 HTML 與 JavaScript，找出使用者能完成的主要流程，設計 3 到 5 個任務。回傳 JSON 陣列，格式：
[{{"prompt":"給受測者看的任務說明","cond":{{"type":"visible 或 click","text":"判斷完成用的文字"}},"path":["第1步要點的按鈕文字","第2步…"]}}]

規則：
- prompt 用情境描述目標，不要透露按鈕名稱或操作步驟。例如「客人想要一杯大杯珍奶、少冰半糖，請幫他點好並送出」，而不是「點擊珍珠奶茶按鈕」。
- cond 必須能在原型中真的被觸發：
  - type=visible：完成後畫面上「才會」出現的文字（例如「訂單已送出」），不能是一開始就看得到的文字。
  - type=click：完成任務的最後一個按鈕上的文字。只有在完成後沒有明確結果畫面時才用。
  - text 要逐字取自 HTML 中實際存在的文字，越短越好但要獨特。
- path 是最短操作路徑，每一步寫該按鈕或選項上實際顯示的文字。
- 任務由簡單到複雜排列，彼此獨立（每個任務開始時原型都會重新載入）。

原型 HTML：
{html}"""


@app.post("/api/studies/<sid>/generate")
@admin_required
def generate(sid):
    row = get_study(sid)
    html = row["html"]
    if len(html) > 120000:
        html = html[:120000] + "\n<!-- 以下省略 -->"
    goal = f"測試目標：{row['goal']}" if row["goal"] else ""
    try:
        out = ai.complete_json(TASK_PROMPT.format(goal=goal, html=html))
    except ai.AIError as e:
        return jsonify(error=str(e)), 502
    tasks = [{"id": new_id("t"), "prompt": str(t.get("prompt", "")),
              "cond": {"type": "click" if (t.get("cond") or {}).get("type") == "click" else "visible",
                       "text": str((t.get("cond") or {}).get("text", ""))},
              "path": [str(p) for p in (t.get("path") or [])][:12]}
             for t in (out if isinstance(out, list) else []) if isinstance(t, dict) and t.get("prompt")][:6]
    db().execute("UPDATE studies SET tasks=? WHERE id=?", (json.dumps(tasks, ensure_ascii=False), sid))
    db().commit()
    return jsonify(tasks=tasks)


@app.post("/api/studies/<sid>/insight")
@admin_required
def insight(sid):
    row = get_study(sid)
    body = request.get_json(silent=True) or {}
    prompt = f"""你是 UX 研究員。以下是餐飲點餐系統原型「{row['name']}」的無主持人易用性測試結果（{body.get('people', 0)} 位受測者）。
請用繁體中文寫給產品團隊看的整理，純文字、不要用 Markdown 符號。結構：
1. 一句話總結。
2. 最嚴重的 3 個問題，每個說明證據（引用數字或點擊位置）和可能原因。
3. 每個問題對應一個具體的設計修改建議。
4. 樣本數少於 5 人時，提醒結論僅供參考。
資料：{json.dumps(body.get('data', []), ensure_ascii=False)[:60000]}"""
    try:
        return jsonify(text=ai.complete(prompt, max_tokens=2000))
    except ai.AIError as e:
        return jsonify(error=str(e)), 502


try:
    init_db()
except Exception as e:  # 資料庫暫時連不上時，讓網站仍能啟動並顯示錯誤
    print("資料庫初始化失敗：", e)

if __name__ == "__main__":
    if not ADMIN_PASSWORD:
        print("警告：尚未設定 ADMIN_PASSWORD，管理介面無法登入")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 8080)))
