"""effluent-bugfix-04 / 争用（zh = zheng）。

争用红线：两路会话**同时删同一父边**必须抢同一把血缘锁；败方零改，只留争用痕
（contention_log）。旧 force-delete 写口根本不取锁，并发双删双双 200，本文件见红。

并发主测用双会话真打同一写口，并对所有时序成立：
- 临界区内撞锁 -> 409 + 争用痕；
- 慢一步赶到 -> 见影子账已知此边被同级联删除 -> 仍 409 + 争用痕，业务零改。
"""
from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db

EDGE_KEY = "lineage-edge:1"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "bf04_zh.db"
    monkeypatch.setattr("db.DB_PATH", db_path)
    init_db()
    yield db_path


def _token(user: str, password: str) -> str:
    c = TestClient(app)
    return c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]


def _auth(user="admin", password="admin123") -> dict:
    return {"Authorization": "Bearer " + _token(user, password)}


def test_zh_delete_respects_held_lineage_lock(_db):
    """确定性争用：血缘锁被他会话持有时，删边口必须 409 且业务零改。"""
    c = TestClient(app)
    admin = _auth("admin", "admin123")
    writer = _auth("writer", "writer123")

    got = c.post("/api/locks/acquire", params={"lock_key": EDGE_KEY}, headers=admin)
    assert got.status_code == 200, got.text

    r = c.post("/api/lineage/1/force-delete", headers=writer)
    assert r.status_code == 409, f"无锁双删埋雷未除：{r.status_code} {r.text}"

    with sqlite3.connect(_db) as conn:
        edge = conn.execute(
            "SELECT id FROM metric_lineage WHERE id=1"
        ).fetchone()
        child = conn.execute(
            "SELECT status FROM sample_batches WHERE id=2"
        ).fetchone()
        traces = conn.execute(
            "SELECT COUNT(*) c FROM contention_log WHERE lock_key=?",
            (EDGE_KEY,),
        ).fetchone()[0]
    assert edge is not None, "败方不得删边"
    assert child[0] != "作废", "败方不得作废子指标"
    assert traces >= 1, "败方必须只记争用痕"


def test_zh_duel_two_sessions_same_edge(_db):
    """duel_cascade_edge：双会话并发打同一边删除口，胜方一笔级联，败方零改。"""
    # 两个独立会话（即便同账号也必须按请求互斥，不能因 holder 同名重入）
    h1 = {"Authorization": "Bearer " + _token("admin", "admin123")}
    h2 = {"Authorization": "Bearer " + _token("admin", "admin123")}

    def duel(headers):
        client = TestClient(app)
        try:
            r = client.post("/api/lineage/1/force-delete", headers=headers)
            return r.status_code, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(duel, h1), pool.submit(duel, h2)]
        results = [f.result(timeout=30) for f in futs]

    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 409], f"必须一成一争，实际：{results}"

    winner_body = next(b for s, b in results if s == 200)
    assert winner_body.get("cascaded") is True
    assert 2 in winner_body.get("invalidated_sample_ids", [])

    with sqlite3.connect(_db) as conn:
        edge = conn.execute("SELECT id FROM metric_lineage WHERE id=1").fetchone()
        child = conn.execute(
            "SELECT status FROM sample_batches WHERE id=2"
        ).fetchone()
        audits = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE action='lineage_force_delete' AND target='1'"
        ).fetchone()[0]
        traces = conn.execute(
            "SELECT actor, action FROM contention_log WHERE lock_key=?",
            (EDGE_KEY,),
        ).fetchall()
        shadows = conn.execute(
            "SELECT COUNT(*) c FROM lineage_shadow_edges WHERE edge_id=1"
        ).fetchone()[0]

    assert edge is None, "胜方必须删掉父边"
    assert child[0] == "作废", "胜方同事务级联作废子指标"
    assert audits == 1, f"败方零改：只允许一笔删边审计，实际 {audits}"
    assert shadows == 1, "只允许一笔影子断链账"
    assert len(traces) >= 1, "败方必须留下争用痕"
    assert any("争用" in (a[1] or "") for a in traces)
