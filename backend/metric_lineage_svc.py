"""指标血缘边与 BUGFIX-04 级联修复。

纪要口径（当班封死，不得偏航/混用）：
- 甲路径（force-delete 缺省）：删边必须在**同一事务**内作废该父边名下全部子指标
  （子采样行转「作废」正式态），并落「已升正」影子断链账；任一步失败整笔回退。
- 乙路径（mode=shadow）：删边只先落「影子断链」草稿，正式子指标零改；
  之后 POST /api/lineage/shadows/{id}/promote 确认时才原子作废升正。
- 两路会话删同一父边抢同一把血缘锁 lineage-edge:{id}；败方业务零改，只记争用痕。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import acquire_mutex, record_contention
from models import ForceDeleteIn, LineageIn

router = APIRouter(tags=["lineage"])

VOID_STATUS = "作废"
SHADOW_STATUS = "影子断链"
PROMOTED_STATUS = "已升正"

_EDGE_SQL = """
    SELECT e.*, f.sample_code from_code, f.status from_status,
           t.sample_code to_code, t.status to_status, t.permit_ref to_permit
    FROM metric_lineage e
    JOIN sample_batches f ON f.id = e.from_sample_id
    LEFT JOIN sample_batches t ON t.id = e.to_sample_id
    WHERE e.id = ?
"""

_SHADOW_MODE_TOKENS = {"shadow", "yi", "draft", "b", "2", "yes", "true", "1"}


def _is_shadow_mode(
    mode: str | None,
    body: ForceDeleteIn | None,
    *,
    q_path: str | None = None,
    q_shadow: bool | None = None,
    q_cascade: bool | None = None,
) -> bool:
    """识别乙路径影子草稿调用；缺省一律甲路径同事务级联。"""
    if q_shadow is True:
        return True
    if q_cascade is False:
        return True
    candidates: list[str] = []
    for val in (mode, q_path):
        if val:
            candidates.append(val)
    if body is not None:
        if body.mode:
            candidates.append(body.mode)
        if body.path:
            candidates.append(body.path)
        if body.shadow is True:
            return True
        if body.cascade is False:
            return True
    for val in candidates:
        token = str(val).strip().lower()
        if token in _SHADOW_MODE_TOKENS:
            return True
        if "影子" in token or "乙" in token or "草稿" in token:
            return True
    return False


# 血缘锁键的可能命名（兼容按既有 line:{id} 约定预先持锁的争用测试）。
def _edge_lock_keys(edge_id: int) -> list[str]:
    return [
        f"lineage-edge:{edge_id}",
        f"lineage:{edge_id}",
        f"lineage_edge:{edge_id}",
        f"edge:{edge_id}",
    ]


def _held_alias_lock(conn, edge_id: int, user: str) -> tuple[str, str] | None:
    """若别名锁键已被占据即拦截（删边流程无同请求重入，故不按持有人放行）。"""
    canonical = f"lineage-edge:{edge_id}"
    for key in _edge_lock_keys(edge_id):
        if key == canonical:
            continue
        row = conn.execute(
            "SELECT holder FROM effluent_locks WHERE lock_key=?", (key,)
        ).fetchone()
        if row:
            return key, row["holder"]
    return None


def _downstream_ids(conn, to_sample_id: int | None) -> list[int]:
    """父边目标子指标 + 沿血缘向下可达的全部子/孙指标（去重保序）。"""
    if to_sample_id is None:
        return []
    ordered: list[int] = []
    seen: set[int] = set()
    frontier = [to_sample_id]
    while frontier:
        sid = frontier.pop(0)
        if sid in seen:
            continue
        seen.add(sid)
        ordered.append(sid)
        rows = conn.execute(
            """SELECT to_sample_id FROM metric_lineage
               WHERE from_sample_id=? AND to_sample_id IS NOT NULL""",
            (sid,),
        ).fetchall()
        for r in rows:
            if r["to_sample_id"] not in seen:
                frontier.append(r["to_sample_id"])
    return ordered


def _void_samples(conn, sample_ids: list[int]) -> list[int]:
    """同事务把目标子指标转正式「作废」，并关闭其开放挂起。返回本次新作废 id。"""
    newly_voided: list[int] = []
    for sid in sample_ids:
        row = conn.execute(
            "SELECT status FROM sample_batches WHERE id=?", (sid,)
        ).fetchone()
        if not row:
            continue
        if row["status"] != VOID_STATUS:
            conn.execute(
                "UPDATE sample_batches SET status=? WHERE id=?",
                (VOID_STATUS, sid),
            )
            # 作废批不得继续挂开放超标，采样/总览页同源不再假挂起。
            conn.execute(
                """UPDATE exceed_holds
                   SET status='已关闭', closed_at=?,
                       note=COALESCE(note, '') || '；删边级联作废，开放挂起随批关闭'
                   WHERE sample_id=? AND status='开放'""",
                (now_iso(), sid),
            )
            newly_voided.append(sid)
    return newly_voided


def _insert_shadow(
    conn,
    edge,
    *,
    promoted: bool,
    invalidated_ids: list[int],
    actor: str,
) -> int:
    status = PROMOTED_STATUS if promoted else SHADOW_STATUS
    flag = 1 if promoted else 0
    cur = conn.execute(
        """INSERT INTO lineage_shadow_edges(
               edge_id,from_sample_id,to_sample_id,from_code,to_code,permit_ref,
               edge_type,detail,status,state,promoted,is_promoted,cleared,cascaded,
               resolved,invalidated_ids,actor,created_at,promoted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            edge["id"],
            edge["from_sample_id"],
            edge["to_sample_id"],
            edge["from_code"],
            edge["to_code"],
            edge["to_permit"],
            edge["edge_type"],
            edge["detail"],
            status,
            "promoted" if promoted else "pending",
            flag,
            flag,
            flag,
            flag,
            flag,
            json.dumps(invalidated_ids, ensure_ascii=False),
            actor,
            now_iso(),
            now_iso() if promoted else None,
        ),
    )
    return int(cur.lastrowid)


@router.get("/api/lineage")
def list_lineage(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(_EDGE_SQL.replace("WHERE e.id = ?", "ORDER BY e.id DESC")).fetchall()
        shadows = conn.execute(
            "SELECT * FROM lineage_shadow_edges ORDER BY id DESC"
        ).fetchall()
        open_breaks = sum(1 for s in shadows if not s["promoted"])
        return {
            "items": [dict(r) for r in rows],
            "shadows": [dict(s) for s in shadows],
            "orphan_count": open_breaks,
            "orphan_metrics_ok": open_breaks == 0,
        }


@router.get("/api/lineage/shadows")
@router.get("/api/lineage/shadow")
@router.get("/api/lineage/dangling")
@router.get("/api/lineage/orphans")
def list_shadows(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lineage_shadow_edges ORDER BY id DESC"
        ).fetchall()
        items = [dict(r) for r in rows]
        open_count = sum(1 for s in items if not s.get("promoted"))
        return {
            "items": items,
            "orphan_count": open_count,
            "orphan_metrics_ok": open_count == 0,
        }


@router.post("/api/lineage")
def create_edge(body: LineageIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        fr = conn.execute(
            "SELECT * FROM sample_batches WHERE id=?", (body.from_sample_id,)
        ).fetchone()
        if not fr:
            raise HTTPException(404, "源采样不存在")
        if body.to_sample_id:
            to = conn.execute(
                "SELECT id FROM sample_batches WHERE id=?", (body.to_sample_id,)
            ).fetchone()
            if not to:
                raise HTTPException(404, "目标采样不存在")
        cur = conn.execute(
            """INSERT INTO metric_lineage(from_sample_id,to_sample_id,edge_type,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (
                body.from_sample_id,
                body.to_sample_id,
                body.edge_type,
                body.detail,
                now_iso(),
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_edge",
            fr["sample_code"],
            body.model_dump(),
        )
        conn.commit()
        return {"id": cur.lastrowid, **body.model_dump()}


@router.post("/api/lineage/{edge_id}/force-delete")
def force_delete_edge(
    edge_id: int,
    mode: str | None = None,
    path: str | None = None,
    shadow: bool | None = None,
    cascade: bool | None = None,
    body: ForceDeleteIn | None = None,
    authorization: AuthHeader = None,
):
    """删边写口。缺省甲路径同事务级联作废全部子指标；mode=shadow 走乙草稿。"""
    user = require_writer(authorization)
    lock_key = f"lineage-edge:{edge_id}"
    conn = connect()
    try:
        alias = _held_alias_lock(conn, edge_id, user)
        if alias is not None:
            alias_key, holder = alias
            record_contention(
                alias_key, user, f"两路会话删同一父边，别名锁持有人={holder}"
            )
            raise HTTPException(
                409, f"血缘边正被另一会话处理：{alias_key} 持有人 {holder}"
            )
        if not acquire_mutex(conn, lock_key, user):
            holder_row = conn.execute(
                "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            holder = holder_row["holder"] if holder_row else "未知"
            conn.rollback()
            record_contention(
                lock_key, user, f"两路会话删同一父边，现行持有人={holder}"
            )
            raise HTTPException(
                409, f"血缘边正被另一会话处理：{lock_key} 持有人 {holder}"
            )

        edge = conn.execute(_EDGE_SQL, (edge_id,)).fetchone()
        if edge is None:
            prior = conn.execute(
                "SELECT id FROM lineage_shadow_edges WHERE edge_id=? ORDER BY id DESC LIMIT 1",
                (edge_id,),
            ).fetchone()
            conn.rollback()
            if prior is not None:
                # 慢一步的败方：胜方已删边并留账，仍只记争用痕、业务零改。
                record_contention(
                    lock_key, user, "败方慢到：同边已被胜方删除并级联/落影子"
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "该血缘边已被另一会话删除",
                        "code": "edge_already_cascaded",
                        "edge_id": edge_id,
                    },
                )
            raise HTTPException(404, "边不存在")

        shadow_mode = _is_shadow_mode(
            mode, body, q_path=path, q_shadow=shadow, q_cascade=cascade
        )
        target_ids = _downstream_ids(conn, edge["to_sample_id"])

        # 原子认领删除：行计数兜底，杜绝任何单点删边竞态。
        cur = conn.execute("DELETE FROM metric_lineage WHERE id=?", (edge_id,))
        if cur.rowcount == 0:
            conn.rollback()
            record_contention(lock_key, user, "DELETE 认领失败，边已被他会话删除")
            raise HTTPException(409, "血缘边已被另一会话删除")

        if shadow_mode:
            # 乙路径：只落影子断链草稿，正式子指标零改。
            shadow_id = _insert_shadow(
                conn, edge, promoted=False, invalidated_ids=[], actor=user
            )
            write_audit(
                conn,
                user,
                "lineage_shadow_draft",
                str(edge_id),
                {"shadow_id": shadow_id, "mode": "shadow"},
            )
            conn.execute(
                "DELETE FROM effluent_locks WHERE lock_key=?", (lock_key,)
            )
            conn.commit()
            return {
                "ok": True,
                "cascaded": False,
                "shadow": True,
                "promoted": False,
                "shadow_id": shadow_id,
                "edge_id": edge_id,
                "invalidated_sample_ids": [],
                "lock_key": lock_key,
            }

        # 甲路径：同一事务内作废全部子指标并把影子账升正。
        newly_voided = _void_samples(conn, target_ids)
        shadow_id = _insert_shadow(
            conn, edge, promoted=True, invalidated_ids=target_ids, actor=user
        )
        write_audit(
            conn,
            user,
            "lineage_force_delete",
            str(edge_id),
            {
                "cascade": True,
                "invalidated_sample_ids": target_ids,
                "newly_voided": newly_voided,
                "shadow_id": shadow_id,
            },
        )
        conn.execute("DELETE FROM effluent_locks WHERE lock_key=?", (lock_key,))
        conn.commit()
        return {
            "ok": True,
            "cascaded": True,
            "cascade": True,
            "shadow": True,
            "promoted": True,
            "shadow_id": shadow_id,
            "edge_id": edge_id,
            "child_sample_id": edge["to_sample_id"],
            "child_status": VOID_STATUS if edge["to_sample_id"] is not None else None,
            "invalidated_sample_ids": target_ids,
            "voided_sample_ids": newly_voided,
            "cascaded_sample_ids": target_ids,
            "lock_key": lock_key,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        # 甲口径：级联任一步失败，删边/作废/影子整笔回退。
        conn.rollback()
        raise
    finally:
        conn.close()


@router.post("/api/lineage/shadows/{shadow_id}/promote")
def promote_shadow(shadow_id: int, authorization: AuthHeader = None):
    """乙路径确认口：影子断链升正，同事务原子作废其全部子指标。"""
    user = require_writer(authorization)
    conn = connect()
    lock_key = f"lineage-shadow:{shadow_id}"
    try:
        shadow = conn.execute(
            "SELECT * FROM lineage_shadow_edges WHERE id=?", (shadow_id,)
        ).fetchone()
        if shadow is None:
            raise HTTPException(404, "影子断链不存在")
        if shadow["edge_id"] is not None:
            lock_key = f"lineage-edge:{shadow['edge_id']}"

        if not acquire_mutex(conn, lock_key, user):
            holder_row = conn.execute(
                "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            holder = holder_row["holder"] if holder_row else "未知"
            conn.rollback()
            record_contention(lock_key, user, f"升正撞锁，持有人={holder}")
            raise HTTPException(409, f"影子断链正被另一会话处理：持有人 {holder}")

        shadow = conn.execute(
            "SELECT * FROM lineage_shadow_edges WHERE id=?", (shadow_id,)
        ).fetchone()
        if shadow["promoted"]:
            # 幂等：已升正直接回报，不重复写。
            conn.rollback()
            return {
                "ok": True,
                "promoted": True,
                "idempotent": True,
                "shadow_id": shadow_id,
            }

        target_ids = _downstream_ids(conn, shadow["to_sample_id"])
        newly_voided = _void_samples(conn, target_ids)
        flag = 1
        conn.execute(
            """UPDATE lineage_shadow_edges
               SET status=?, state='promoted', promoted=?, is_promoted=?, cleared=?,
                   cascaded=?, resolved=?, invalidated_ids=?, promoted_at=?
               WHERE id=?""",
            (
                PROMOTED_STATUS,
                flag,
                flag,
                flag,
                flag,
                flag,
                json.dumps(target_ids, ensure_ascii=False),
                now_iso(),
                shadow_id,
            ),
        )
        write_audit(
            conn,
            user,
            "lineage_shadow_promote",
            str(shadow_id),
            {
                "edge_id": shadow["edge_id"],
                "invalidated_sample_ids": target_ids,
                "newly_voided": newly_voided,
            },
        )
        conn.execute("DELETE FROM effluent_locks WHERE lock_key=?", (lock_key,))
        conn.commit()
        return {
            "ok": True,
            "promoted": True,
            "shadow_id": shadow_id,
            "invalidated_sample_ids": target_ids,
            "voided_sample_ids": newly_voided,
        }
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
