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
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, abort, g, jsonify, request, send_from_directory, session

import ai

ROOT = Path(__file__).parent
ON_VERCEL = bool(os.environ.get("VERCEL"))
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DATA_DIR = Path(os.environ.get("DATA_DIR") or ("/tmp/ut-data" if ON_VERCEL else ROOT / "data"))
DB_PATH = DATA_DIR / "app.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_HTML = 4 * 1024 * 1024          # Vercel 單次請求上限約 4.5 MB
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
    "CREATE INDEX IF NOT EXISTS idx_sessions_study ON sessions(study_id)",
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


def get_study(sid):
    row = db().execute("SELECT * FROM studies WHERE id=?", (sid,)).fetchone()
    if not row:
        abort(404)
    return row


def study_json(row, with_html=False):
    d = {"id": row["id"], "name": row["name"], "goal": row["goal"], "createdAt": row["created_at"],
         "tasks": json.loads(row["tasks"] or "[]"), "size": len(row["html"] or "")}
    if with_html:
        d["html"] = row["html"]
    return d


# ---------- 登入 ----------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("admin"):
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
    if not ADMIN_PASSWORD or not hmac.compare_digest(pw, ADMIN_PASSWORD):
        _fails[ip] = recent + [time.time()]
        return jsonify(error="密碼錯誤" if ADMIN_PASSWORD else "伺服器尚未設定 ADMIN_PASSWORD"), 401
    session["admin"] = True
    session.permanent = True
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
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
    return jsonify(name=row["name"], tasks=tasks, html=row["html"])


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
    data = {"role": str(data.get("role", ""))[:20], "onsite": bool(data.get("onsite")),
            "device": data.get("device") if isinstance(data.get("device"), dict) else {},
            "results": data["results"][:20]}
    sid_ = new_id("x")
    db().execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (sid_, sid, data["role"], json.dumps(data, ensure_ascii=False), now()))
    db().commit()
    return jsonify(ok=True, id=sid_)


# ---------- 管理者 ----------

@app.get("/api/me")
def me():
    return jsonify(admin=bool(session.get("admin")), aiReady=ai.configured(), provider=ai.PROVIDER, model=ai.MODEL,
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
                      "cond": {"type": "click" if cond.get("type") == "click" else "visible", "text": str(cond.get("text", ""))[:200]},
                      "path": [str(p)[:80] for p in (t.get("path") or [])][:15]})
    db().execute("UPDATE studies SET tasks=? WHERE id=?", (json.dumps(clean, ensure_ascii=False), sid))
    db().commit()
    return jsonify(tasks=clean)


@app.delete("/api/studies/<sid>")
@admin_required
def delete_study(sid):
    db().execute("DELETE FROM sessions WHERE study_id=?", (sid,))
    db().execute("DELETE FROM studies WHERE id=?", (sid,))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/studies/<sid>/sessions")
@admin_required
def list_sessions(sid):
    rows = db().execute("SELECT * FROM sessions WHERE study_id=? ORDER BY created_at", (sid,)).fetchall()
    return jsonify([{**json.loads(r["data"]), "id": r["id"], "at": r["created_at"]} for r in rows])


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
