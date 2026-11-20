
import hashlib, secrets, sqlite3
from pathlib import Path
from fastapi import Header, HTTPException
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "实体键桥"
SKELETON = "实体键映射生效后入站先重映再进正式区，冲突进队列，映射版本与正式区对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS map_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL,
          effective_from TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mappings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          version TEXT NOT NULL,
          old_key TEXT NOT NULL,
          new_key TEXT NOT NULL,
          kind TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS formal_entities(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          entity_key TEXT NOT NULL,
          mapped_from TEXT NOT NULL,
          version TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conflicts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tombstones(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT UNIQUE NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          ledger_version TEXT NOT NULL,
          ledger_new_key TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refuse_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          version TEXT NOT NULL,
          target_key TEXT NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS key_locks(
          old_key TEXT PRIMARY KEY,
          holder TEXT NOT NULL,
          acquired_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS key_contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          holder TEXT NOT NULL,
          reason TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tombstone_deviation(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          old_key TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM map_versions").fetchone()["c"] == 0:
            conn.execute("INSERT INTO map_versions(version,status,effective_from) VALUES (?,?,?)", ("m-2026.11","生效","2026-11-01"))
            conn.executemany("INSERT INTO mappings(version,old_key,new_key,kind) VALUES (?,?,?,?)", [
                ("m-2026.11","OLD-A1","NEW-100","一对一"),
                ("m-2026.11","OLD-B1","NEW-200","多对一"),
                ("m-2026.11","OLD-B2","NEW-200","多对一"),
            ])
            conn.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                         ("NEW-100","OLD-A1","m-2026.11","2026-11-10T09:00:00"))
            conn.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", ("OLD-C9","无映射","挂起"))

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM mappings").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "mappings": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/map-versions")
def versions(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM map_versions ORDER BY id").fetchall()]}

@app.get("/api/mappings")
def mappings(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM mappings ORDER BY id").fetchall()]}

class RemapIn(BaseModel):
    old_key: str
    force: bool = False
    mode: str = "禁写"

class TombstoneIn(BaseModel):
    old_key: str
    reason: str = "旧键废弃"

# 墓碑修复册封册口径：甲路径（禁写），乙路径（改键审计）不采用。
REPAIR_BOOK = {"mode": "禁写", "on_tombstone": "整笔拒绝+正式零改+拒映审计"}

def _deviation(c, old_key: str, detail: str, now: str) -> None:
    c.execute("INSERT INTO tombstone_deviation(old_key,detail,created_at) VALUES (?,?,?)",
              (old_key, detail, now))
    c.commit()

def require_admin(authorization: str | None) -> str:
    u = require_user(authorization)
    with connect() as c:
        row = c.execute("SELECT role FROM users WHERE username=?", (u,)).fetchone()
    if not row or row["role"] != "管理员":
        raise HTTPException(403, "仅管理员可写墓碑")
    return u

def _acquire_key_lock(c, old_key: str, holder: str, user: str, now: str) -> None:
    """占旧键锁：锁行先提交以便并发方可见；后到者只记争用，不再写正式区或墓碑。"""
    try:
        c.execute("INSERT INTO key_locks(old_key,holder,acquired_at) VALUES (?,?,?)",
                  (old_key, holder, now))
        c.commit()
    except sqlite3.IntegrityError:
        c.rollback()
        c.execute("INSERT INTO key_contention(old_key,holder,reason,created_at) VALUES (?,?,?,?)",
                  (old_key, user, "旧键锁被占，争用", now))
        c.commit()
        raise HTTPException(409, "旧键已被占用，争用已记录")

def _release_key_lock(c, old_key: str, holder: str) -> None:
    c.execute("DELETE FROM key_locks WHERE old_key=? AND holder=?", (old_key, holder))
    c.commit()

@app.post("/api/remap")
def remap(body: RemapIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    holder = secrets.token_hex(8)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")

        # 偏离修复册：force 或非禁写口径，一律拒写并记「墓碑偏了」。
        if body.force or body.mode != REPAIR_BOOK["mode"]:
            _deviation(c, body.old_key, f"偏离修复册：force={body.force} mode={body.mode}", now)
            raise HTTPException(409, "墓碑偏了：偏离修复册口径，拒写")

        _acquire_key_lock(c, body.old_key, holder, user, now)
        try:
            # 甲路径（禁写）：旧键已墓碑只读 → 重映整笔拒绝，正式零改，写拒映审计。
            tomb = c.execute("SELECT * FROM tombstones WHERE old_key=? AND status='只读'",
                             (body.old_key,)).fetchone()
            if tomb:
                c.execute("UPDATE tombstones SET attempts=attempts+1 WHERE old_key=?", (body.old_key,))
                c.execute(
                    "INSERT INTO refuse_audit(old_key,version,target_key,reason,status,created_at) VALUES (?,?,?,?,?,?)",
                    (body.old_key, ver["version"], tomb["ledger_new_key"], "墓碑旧键只读，重映拒映", "拒映", now))
                c.commit()
                raise HTTPException(409, "墓碑旧键只读，重映拒映")

            m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
            if not m:
                c.execute("INSERT INTO conflicts(old_key,reason,status) VALUES (?,?,?)", (body.old_key, "无映射", "挂起"))
                c.commit()
                raise HTTPException(409, "无映射，已进冲突队列")
            c.execute("INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
                      (m["new_key"], body.old_key, ver["version"], now))
            c.commit()
            return {"entity_key": m["new_key"], "version": ver["version"]}
        finally:
            _release_key_lock(c, body.old_key, holder)

@app.post("/api/tombstones")
def create_tombstone(body: TombstoneIn, authorization: str | None = Header(default=None)):
    user = require_admin(authorization)
    holder = secrets.token_hex(8)
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as c:
        ver = c.execute("SELECT version FROM map_versions WHERE status='生效' ORDER BY id DESC LIMIT 1").fetchone()
        if not ver: raise HTTPException(404, "无生效映射版本")
        m = c.execute("SELECT * FROM mappings WHERE version=? AND old_key=?", (ver["version"], body.old_key)).fetchone()
        if not m: raise HTTPException(409, "无映射，不能立墓碑")

        _acquire_key_lock(c, body.old_key, holder, user, now)
        try:
            exists = c.execute("SELECT 1 FROM tombstones WHERE old_key=?", (body.old_key,)).fetchone()
            if exists:
                raise HTTPException(409, "旧键已有墓碑")
            c.execute(
                "INSERT INTO tombstones(old_key,reason,status,ledger_version,ledger_new_key,attempts,created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (body.old_key, body.reason, "只读", ver["version"], m["new_key"], 0, now))
            c.commit()
            return {"old_key": body.old_key, "status": "只读", "ledger_new_key": m["new_key"]}
        finally:
            _release_key_lock(c, body.old_key, holder)

@app.get("/api/formal")
def formal(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM formal_entities ORDER BY id DESC").fetchall()]}

@app.get("/api/conflicts")
def conflicts(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM conflicts ORDER BY id DESC").fetchall()]}

@app.get("/api/tombstones")
def tombstones(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM tombstones ORDER BY id DESC").fetchall()]}

@app.get("/api/refuse-audit")
def refuse_audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM refuse_audit ORDER BY id DESC").fetchall()]}

@app.get("/api/contention")
def contention(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM key_contention ORDER BY id DESC").fetchall()]}

@app.get("/api/cross-check")
def cross_check(authorization: str | None = Header(default=None)):
    """互证页：墓碑 / 正式实体 / 拒映审计 / 映射册 五处同态核对。"""
    require_user(authorization)
    items = []
    with connect() as c:
        tombs = c.execute("SELECT * FROM tombstones ORDER BY id").fetchall()
        for t in tombs:
            old_key = t["old_key"]
            formal_hits = c.execute(
                "SELECT COUNT(*) n FROM formal_entities WHERE mapped_from=?", (old_key,)).fetchone()["n"]
            mappings = c.execute("SELECT new_key FROM mappings WHERE old_key=?", (old_key,)).fetchall()
            mapping_keys = {m["new_key"] for m in mappings}
            audits = c.execute(
                "SELECT * FROM refuse_audit WHERE old_key=? ORDER BY id DESC", (old_key,)).fetchall()
            audit = audits[0] if audits else None
            ledger_ok = t["ledger_new_key"] in mapping_keys
            audit_ok = bool(audit) and audit["version"] == t["ledger_version"] and audit["target_key"] == t["ledger_new_key"]
            consistent = t["status"] == "只读" and formal_hits == 0 and ledger_ok and audit_ok
            items.append({
                "old_key": old_key,
                "status": t["status"],
                "ledger_new_key": t["ledger_new_key"],
                "formal_leak": formal_hits,
                "ledger_consistent": ledger_ok,
                "audit_status": audit["status"] if audit else None,
                "audit_consistent": audit_ok,
                "consistent": consistent,
            })
    return {"ok": all(i["consistent"] for i in items), "repair_book": REPAIR_BOOK, "items": items}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
