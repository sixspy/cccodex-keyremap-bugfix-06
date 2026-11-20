"""墓碑暗写回归：旧键已墓碑只读后，重映不得再改正式实体键。

先落红测，见红后才允许修改墓碑相关写路径。
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

import app as appmod


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    appmod.init_db()
    yield


def _login(client):
    tok = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _raw():
    conn = sqlite3.connect(appmod.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_tombstone(old_key, reason="旧键废弃"):
    conn = _raw()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tombstones(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_key TEXT UNIQUE NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            ledger_version TEXT NOT NULL,
            ledger_new_key TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT OR IGNORE INTO tombstones(old_key,reason,status,ledger_version,ledger_new_key,created_at)"
        " VALUES (?,?,?,?,?,?)",
        (old_key, reason, "只读", "m-2026.11", "NEW-200", "2026-11-19T10:00:00"),
    )
    conn.commit()
    conn.close()


def _seed_lock(old_key, holder="并发写者"):
    conn = _raw()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS key_locks(
            old_key TEXT PRIMARY KEY,
            holder TEXT NOT NULL,
            acquired_at TEXT NOT NULL)"""
    )
    conn.execute(
        "INSERT OR REPLACE INTO key_locks(old_key,holder,acquired_at) VALUES (?,?,?)",
        (old_key, holder, "2026-11-19T10:00:01"),
    )
    conn.commit()
    conn.close()


def _formal_rows(old_key=None):
    conn = _raw()
    if old_key is None:
        rows = conn.execute("SELECT COUNT(*) c FROM formal_entities").fetchone()["c"]
    else:
        rows = conn.execute(
            "SELECT COUNT(*) c FROM formal_entities WHERE mapped_from=?", (old_key,)
        ).fetchone()["c"]
    conn.close()
    return rows


# ---- 红测 1：墓碑后重映仍改键（暗写）----
def test_tombstoned_key_remap_is_rejected_and_formal_untouched():
    _seed_tombstone("OLD-B2")
    client = TestClient(appmod.app)
    h = _login(client)
    before = _formal_rows()

    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B2"})

    assert r.status_code == 409, f"墓碑后重映必须整笔拒绝，实际 {r.status_code} {r.text}"
    assert "墓碑" in r.json()["detail"] or "拒映" in r.json()["detail"]
    assert _formal_rows() == before, "墓碑后正式实体必须零改"
    assert _formal_rows("OLD-B2") == 0, "不得留下墓碑旧键的正式实体"


# ---- 红测 2：旧键锁被占，后到只记争用 ----
def test_remap_while_old_key_lock_held_records_contention_only():
    _seed_lock("OLD-B1")
    client = TestClient(appmod.app)
    h = _login(client)
    before = _formal_rows()

    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B1"})

    assert r.status_code == 409, f"抢写后到必须记争用，实际 {r.status_code} {r.text}"
    assert "争用" in r.json()["detail"]
    assert _formal_rows() == before, "争用后到者不得写正式区"


# ---- 五处同态：墓碑 / 正式 / 拒映审计 / 映射册一致 ----
def test_tombstone_refusal_keeps_five_ledgers_consistent():
    _seed_tombstone("OLD-B2")
    client = TestClient(appmod.app)
    h = _login(client)

    client.post("/api/remap", headers=h, json={"old_key": "OLD-B2"})

    tomb = client.get("/api/tombstones", headers=h).json()["items"][0]
    audit = client.get("/api/refuse-audit", headers=h).json()["items"][0]
    formal = client.get("/api/formal", headers=h).json()["items"]
    mappings = client.get("/api/mappings", headers=h).json()["items"]

    assert tomb["status"] == "只读"
    assert audit["status"] == "拒映"
    assert audit["old_key"] == tomb["old_key"]
    assert audit["version"] == tomb["ledger_version"]
    assert audit["target_key"] == tomb["ledger_new_key"]
    assert not any(f["mapped_from"] == "OLD-B2" for f in formal), "正式区不得出现墓碑旧键"
    mapped = [m for m in mappings if m["old_key"] == "OLD-B2"][0]
    assert mapped["new_key"] == tomb["ledger_new_key"] == audit["target_key"]



# ---- 墓碑写入与重映并发须占旧键锁，后到只记争用 ----
def test_tombstone_write_and_remap_share_old_key_lock():
    _seed_lock("OLD-A1")
    client = TestClient(appmod.app)
    h = _login(client)
    before = _formal_rows("OLD-A1")

    r1 = client.post("/api/tombstones", headers=h, json={"old_key": "OLD-A1"})
    r2 = client.post("/api/remap", headers=h, json={"old_key": "OLD-A1"})

    assert r1.status_code == 409 and "争用" in r1.json()["detail"]
    assert r2.status_code == 409 and "争用" in r2.json()["detail"]
    assert _formal_rows("OLD-A1") == before
    conn = _raw()
    assert conn.execute("SELECT COUNT(*) c FROM tombstones").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM key_contention").fetchone()["c"] == 2
    conn.close()


# ---- 偏离修复册：拒写并记「墓碑偏了」----
def test_deviation_from_repair_book_is_refused_and_recorded():
    _seed_tombstone("OLD-B2")
    client = TestClient(appmod.app)
    h = _login(client)
    before = _formal_rows()

    r = client.post("/api/remap", headers=h, json={"old_key": "OLD-B2", "force": True})

    assert r.status_code == 409, f"偏离修复册必须拒写，实际 {r.status_code} {r.text}"
    assert "墓碑偏了" in r.json()["detail"]
    assert _formal_rows() == before
    conn = _raw()
    dev = conn.execute("SELECT * FROM tombstone_deviation WHERE old_key='OLD-B2'").fetchall()
    assert dev, "偏离须记入墓碑偏了台账"
    conn.close()


# ---- 互证页数据：五处同态可查 ----
def test_cross_check_reports_five_ledgers_in_sync():
    _seed_tombstone("OLD-B2")
    client = TestClient(appmod.app)
    h = _login(client)
    client.post("/api/remap", headers=h, json={"old_key": "OLD-B2"})

    j = client.get("/api/cross-check", headers=h).json()
    assert j["ok"] is True, j
    assert j["items"][0]["old_key"] == "OLD-B2"
    assert j["items"][0]["consistent"] is True


def test_cross_check_flags_formal_leak_as_inconsistent():
    _seed_tombstone("OLD-B2")
    conn = _raw()
    conn.execute(
        "INSERT INTO formal_entities(entity_key,mapped_from,version,created_at) VALUES (?,?,?,?)",
        ("NEW-200", "OLD-B2", "m-2026.11", "2026-11-20T00:00:00"),
    )
    conn.commit(); conn.close()

    client = TestClient(appmod.app)
    h = _login(client)
    j = client.get("/api/cross-check", headers=h).json()
    assert j["ok"] is False, j
    assert j["items"][0]["consistent"] is False
