"""effluent-bugfix-04 / 级联甲（jia）。

甲路径口径：删边须在**同一事务**内作废该父边名下全部子指标（子采样行转「作废」
正式态），任何一步失败整笔回退，不得「只删边留子」。

本文件先于修复写入，在埋雷代码上必须见红：
- force-delete 仅 DELETE metric_lineage，子采样依旧正式；
- 返回 cascaded=False。
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import app
from db import DB_PATH, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    db_path = tmp_path / "bf04_jia.db"
    monkeypatch.setattr("db.DB_PATH", db_path)
    init_db()
    yield db_path


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _connect(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _parent_edge_with_child(c: TestClient, h: dict) -> dict:
    edges = c.get("/api/lineage", headers=h).json()["items"]
    edge = next(e for e in edges if e["to_sample_id"] is not None)
    return edge


def test_jia_force_delete_cascades_child_to_void(_db):
    """甲红点：边已删，子指标不得仍留正式册，必须同事务正式作废。"""
    c = TestClient(app)
    h = _auth(c)

    edge = _parent_edge_with_child(c, h)
    child_id = edge["to_sample_id"]

    before = c.get(f"/api/samples/{child_id}", headers=h).json()["sample"]
    assert before["status"] != "作废"  # 夹具前提：子指标在正式册

    r = c.post(f"/api/lineage/{edge['id']}/force-delete", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    # 删边写口必须回报已级联，而不是旧埋雷的 cascaded=False
    assert body.get("cascaded") is True
    assert child_id in body.get("invalidated_sample_ids", [])

    # 边确已删除
    edges = c.get("/api/lineage", headers=h).json()["items"]
    assert all(e["id"] != edge["id"] for e in edges)

    # 子指标正式作废
    after = c.get(f"/api/samples/{child_id}", headers=h).json()["sample"]
    assert after["status"] == "作废"

    # 库内核对，防止只改响应不改状态
    with _connect(_db) as conn:
        row = conn.execute(
            "SELECT status FROM sample_batches WHERE id=?", (child_id,)
        ).fetchone()
        assert row["status"] == "作废"


def test_jia_cascade_is_atomic_or_full_rollback(_db, monkeypatch):
    """甲口径「否则整笔回退」：级联途中报错，边与子指标都必须保持原样。"""
    import metric_lineage_svc

    def _boom(*_a, **_k):
        raise RuntimeError("bf04 injected cascade failure")

    monkeypatch.setattr(metric_lineage_svc, "write_audit", _boom)

    c = TestClient(app, raise_server_exceptions=False)
    h = _auth(c)
    edge = _parent_edge_with_child(c, h)
    child_id = edge["to_sample_id"]

    r = c.post(f"/api/lineage/{edge['id']}/force-delete", headers=h)
    assert r.status_code == 500

    with _connect(_db) as conn:
        edge_row = conn.execute(
            "SELECT id FROM metric_lineage WHERE id=?", (edge["id"],)
        ).fetchone()
        child = conn.execute(
            "SELECT status FROM sample_batches WHERE id=?", (child_id,)
        ).fetchone()
        lock = conn.execute(
            "SELECT 1 FROM effluent_locks WHERE lock_key=?",
            (f"lineage-edge:{edge['id']}",),
        ).fetchone()
    assert edge_row is not None, "级联失败必须整笔回退，边不得被单点删除"
    assert child["status"] != "作废", "回退后子指标必须保持正式"
    assert lock is None, "回退后不得残留血缘锁"


def test_jia_cascades_entire_downstream_subtree(_db):
    """「作废全部子指标」：沿血缘向下的子/孙批一并作废，不漏子指标行。"""
    c = TestClient(app)
    h = _auth(c)

    def mk_sample(code: str) -> int:
        r = c.post(
            "/api/samples",
            headers=h,
            json={
                "outfall_id": 1,
                "permit_ref": "DP-2026-E01",
                "sample_code": code,
                "flow_m3h": 100,
                "metrics": {"cod": 20.0, "nh3n": 1.0, "tp": 0.1},
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    root = mk_sample("SM-BF04-JIA-R")
    child = mk_sample("SM-BF04-JIA-C")
    grand = mk_sample("SM-BF04-JIA-G")

    def mk_edge(a: int, b: int) -> int:
        r = c.post(
            "/api/lineage",
            headers=h,
            json={
                "from_sample_id": a,
                "to_sample_id": b,
                "edge_type": "复测续批",
                "detail": "bf04 jia subtree",
            },
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    target_edge = mk_edge(root, child)
    mk_edge(child, grand)

    r = c.post(f"/api/lineage/{target_edge}/force-delete", headers=h)
    assert r.status_code == 200, r.text
    ids = r.json()["invalidated_sample_ids"]
    assert child in ids and grand in ids

    for sid in (child, grand):
        s = c.get(f"/api/samples/{sid}", headers=h).json()["sample"]
        assert s["status"] == "作废"
