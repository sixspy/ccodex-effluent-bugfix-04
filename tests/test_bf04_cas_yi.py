"""effluent-bugfix-04 / 级联乙（yi）。

乙路径的正确性红线：删边先落「影子断链」草稿时，正式子指标零改；在影子
**未升正**（未确认原子作废）之前，对账服务不得把它当作「已清/无孤儿」而假绿。

本文件先于修复写入，在埋雷代码上必须见红：
- 旧 force-delete 不认 shadow 模式，但仍会硬删边、留子指标正式；
- 旧 reconcile 把 orphan_metrics_ok 硬编码为 True（误标已清）。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "bf04_yi.db"
    monkeypatch.setattr("db.DB_PATH", db_path)
    init_db()
    yield db_path


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_yi_unpromoted_shadow_is_not_cleared_by_reconcile(_db):
    """影子断链未升正，对账必须查破假绿，且不计该孤儿指标。"""
    c = TestClient(app)
    h = _auth(c)

    edges = c.get("/api/lineage", headers=h).json()["items"]
    edge = next(e for e in edges if e["to_sample_id"] is not None)
    child_id = edge["to_sample_id"]
    child_code = edge["to_code"]

    # 乙路径：只落影子断链草稿，正式子指标零改
    r = c.post(
        f"/api/lineage/{edge['id']}/force-delete?mode=shadow", headers=h
    )
    assert r.status_code == 200, r.text

    child = c.get(f"/api/samples/{child_id}", headers=h).json()["sample"]
    assert child["status"] != "作废", "乙路径未确认前正式子指标必须零改"

    rec = c.get("/api/reconcile/DP-2026-E01", headers=h).json()
    # 核心红点：不得误标 orphan_metrics_ok=True（旧代码硬编码 True）
    assert rec["orphan_metrics_ok"] is False
    assert rec["orphan_count"] >= 1
    orphan_codes = {o["sample_code"] for o in rec["orphan_metrics"]}
    assert child_code in orphan_codes
    # 「对账不计孤儿指标」：孤儿不得留在被计价的 samples 集合里
    counted_codes = {s["sample_code"] for s in rec["samples"]}
    assert child_code not in counted_codes
    # 假绿不得传导到证明态
    assert rec["proof_ready"] is False


def test_yi_shadow_promotion_atomically_voids_and_clears(_db):
    """乙路径确认后原子作废，影子升正，对账才恢复 orphan_metrics_ok=True。"""
    c = TestClient(app)
    h = _auth(c)

    edges = c.get("/api/lineage", headers=h).json()["items"]
    edge = next(e for e in edges if e["to_sample_id"] is not None)
    child_id = edge["to_sample_id"]

    r = c.post(
        f"/api/lineage/{edge['id']}/force-delete?mode=shadow", headers=h
    )
    shadow_id = r.json().get("shadow_id")
    assert shadow_id, "shadow 模式必须回传 shadow_id"

    p = c.post(f"/api/lineage/shadows/{shadow_id}/promote", headers=h)
    assert p.status_code == 200, p.text
    assert p.json().get("promoted") is True

    child = c.get(f"/api/samples/{child_id}", headers=h).json()["sample"]
    assert child["status"] == "作废"

    rec = c.get("/api/reconcile/DP-2026-E01", headers=h).json()
    assert rec["orphan_metrics_ok"] is True
    assert rec["orphan_count"] == 0


def test_yi_reconcile_detects_unpromoted_shadow_row_directly(_db):
    """分服务救绿：对账服务只认影子账，不依赖删边口怎么写。"""
    c = TestClient(app)
    h = _auth(c)

    with sqlite3.connect(_db) as conn:
        conn.execute(
            """INSERT INTO lineage_shadow_edges
               (edge_id,from_sample_id,to_sample_id,from_code,to_code,
                permit_ref,edge_type,detail,status,promoted,cascaded,actor,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                99904,
                1,
                2,
                "SM-OF-E1-A-001",
                "SM-OF-E1-A-002",
                "DP-2026-E01",
                "复测续批",
                "bf04 yi direct shadow",
                "影子断链",
                0,
                0,
                "tester",
                "2026-11-25T16:00:00",
            ),
        )
        conn.commit()

    rec = c.get("/api/reconcile/DP-2026-E01", headers=h).json()
    assert rec["orphan_metrics_ok"] is False
    assert any(
        o["sample_code"] == "SM-OF-E1-A-002" for o in rec["orphan_metrics"]
    )
