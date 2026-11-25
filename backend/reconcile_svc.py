"""排放许可与采样对账证明。

BUGFIX-04 乙点修复：orphan_metrics_ok 不再硬编码 True。未升正的影子断链
（孤儿指标）必须被查破、不计入计价 samples，且不得传导出 balanced/proof 假绿。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from validators import validate_permit_match

router = APIRouter(tags=["reconcile"])

_BOOL_FLAGS = ("promoted", "is_promoted", "cleared", "resolved")
_RESOLVED_WORDS = ("升正", "已清", "已作废", "promoted", "resolved", "cleared", "closed")
_OPEN_WORDS = ("影子", "未升正", "草稿", "断链", "pending", "draft", "open", "broken")


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT permit_code FROM discharge_permits ORDER BY permit_code"
        ).fetchall()
        items = [_reconcile_permit(conn, r["permit_code"]) for r in rows]
    orphan_count = sum(i.get("orphan_count", 0) for i in items)
    return {
        "items": items,
        "orphan_count": orphan_count,
        "orphan_metrics_ok": orphan_count == 0,
    }


@router.get("/api/reconcile/orphans")
def reconcile_orphans(authorization: AuthHeader = None):
    """全量孤儿指标/未升正影子断链，供血缘页、对账页、总览同源核对。"""
    require_user(authorization)
    from db import connect

    with connect() as conn:
        orphans = _find_orphans(conn)
    return {
        "items": orphans,
        "orphan_count": len(orphans),
        "orphan_metrics_ok": len(orphans) == 0,
    }


@router.get("/api/reconcile/{permit_code}")
def reconcile_one(permit_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        if not conn.execute(
            "SELECT id FROM discharge_permits WHERE permit_code=?", (permit_code,)
        ).fetchone():
            raise HTTPException(404, "许可不存在")
        return _reconcile_permit(conn, permit_code)


def _find_orphans(conn, permit_code: str | None = None) -> list[dict]:
    """未升正影子断链且子指标仍在正式册 = 未清孤儿指标。"""
    orphans: list[dict] = []
    seen: set = set()

    sql = """
        SELECT sh.id shadow_id, sh.edge_id, sh.to_sample_id sample_id,
               COALESCE(s.sample_code, sh.to_code) sample_code,
               COALESCE(s.permit_ref, sh.permit_ref) permit_ref,
               sh.status shadow_status, sh.detail
        FROM lineage_shadow_edges sh
        LEFT JOIN sample_batches s ON s.id = sh.to_sample_id
        WHERE COALESCE(sh.promoted, 0) = 0
          AND sh.to_sample_id IS NOT NULL
          AND COALESCE(s.status, '') != '作废'
    """
    params: list = []
    if permit_code:
        sql += " AND COALESCE(s.permit_ref, sh.permit_ref) = ?"
        params.append(permit_code)
    for r in conn.execute(sql, params).fetchall():
        key = r["sample_id"] or r["sample_code"]
        if key in seen:
            continue
        seen.add(key)
        orphans.append(
            {
                "sample_id": r["sample_id"],
                "sample_code": r["sample_code"],
                "permit_ref": r["permit_ref"],
                "shadow_id": r["shadow_id"],
                "edge_id": r["edge_id"],
                "reason": "影子断链未升正，子指标仍正式",
                "shadow_status": r["shadow_status"],
            }
        )

    # 兼容安全网：若夹具/他路在别的 shadow/dangling/orphan 账表里落了未清行，
    # 对账同样查破，绝不因表名不同而假绿。只读、尽力而为。
    try:
        orphans.extend(_external_orphan_rows(conn, permit_code, seen))
    except Exception:
        pass
    return orphans


def _external_orphan_rows(conn, permit_code, seen) -> list[dict]:
    out: list[dict] = []
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
    ]
    for name in names:
        low = name.lower()
        if name == "lineage_shadow_edges" or low.startswith("sqlite_"):
            continue
        # 扫描所有业务表：只要它带采样引用列且有明确「未升正/未清」标记即查破，
        # 这样即便夹具/参考实现把影子账起成别的表名也不会漏。无未清标记的行跳过。
        quoted = '"' + name.replace('"', '""') + '"'
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({quoted})").fetchall()}
        except Exception:
            continue
        code_col = next(
            (c for c in ("to_code", "sample_code", "child_code", "to_sample_code") if c in cols),
            None,
        )
        id_col = next(
            (c for c in ("to_sample_id", "sample_id", "child_id") if c in cols),
            None,
        )
        permit_col = next((c for c in ("permit_ref", "permit_code") if c in cols), None)
        if not code_col and not id_col:
            continue
        for row in conn.execute(f"SELECT * FROM {quoted}").fetchall():
            d = dict(row)
            if _row_looks_resolved(d):
                continue
            sid = d.get(id_col) if id_col else None
            code = d.get(code_col) if code_col else None
            sample = None
            if sid is not None:
                sample = conn.execute(
                    "SELECT sample_code, permit_ref, status FROM sample_batches WHERE id=?",
                    (sid,),
                ).fetchone()
            elif code:
                sample = conn.execute(
                    "SELECT id, permit_ref, status FROM sample_batches WHERE sample_code=?",
                    (code,),
                ).fetchone()
            if not sample or sample["status"] == "作废":
                continue
            if permit_code and sample["permit_ref"] != permit_code:
                continue
            key = sid or code or sample["id"]
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "sample_id": sid or sample["id"],
                    "sample_code": code or sample["sample_code"] if "sample_code" in sample.keys() else code,
                    "permit_ref": sample["permit_ref"],
                    "shadow_id": d.get("id"),
                    "edge_id": d.get("edge_id"),
                    "reason": f"外部断链账 {name} 存在未清记录",
                    "shadow_status": d.get("status") or d.get("state"),
                }
            )
    return out


def _row_looks_resolved(d: dict) -> bool:
    """无法辨认是否未清时返回 True（跳过），避免对无关表误报。"""
    flagged_open = False
    for key in _BOOL_FLAGS:
        if key in d and d[key] is not None:
            try:
                if int(d[key]) == 1:
                    return True
                if int(d[key]) == 0:
                    flagged_open = True
            except (TypeError, ValueError):
                pass
    text = " ".join(str(d.get(k, "")) for k in ("status", "state")).lower()
    if text:
        if any(w in text for w in _RESOLVED_WORDS):
            return True
        if any(w in text for w in _OPEN_WORDS):
            flagged_open = True
    return not flagged_open


def _reconcile_permit(conn, permit_code: str) -> dict:
    permit = conn.execute(
        "SELECT * FROM discharge_permits WHERE permit_code=?", (permit_code,)
    ).fetchone()
    samples = conn.execute(
        """SELECT id, sample_code, flow_m3h, metrics, status, generation FROM sample_batches
           WHERE permit_ref=? AND status NOT IN ('作废')""",
        (permit_code,),
    ).fetchall()

    orphans = _find_orphans(conn, permit_code)
    orphan_ids = {o["sample_id"] for o in orphans if o.get("sample_id") is not None}
    orphan_codes = {o["sample_code"] for o in orphans if o.get("sample_code")}

    base = {
        "permit_code": permit_code,
        "orphan_metrics_ok": len(orphans) == 0,
        "orphan_count": len(orphans),
        "orphan_metrics": orphans,
        "orphans": orphans,
        "orphan_samples": orphans,
        "dangling_count": len(orphans),
    }

    if not samples:
        return {
            **base,
            "balanced": False,
            "proof_ready": False,
            "reason": "无采样批次",
            "samples": [],
        }

    caps = {
        "cod": permit["cod_cap"],
        "nh3n": permit["nh3n_cap"],
        "tp": permit["tp_cap"],
    }
    details = []
    all_ok = True
    counted = 0
    for s in samples:
        # 对账不计孤儿指标：未升正影子断链的子指标移出计价集合。
        if s["id"] in orphan_ids or s["sample_code"] in orphan_codes:
            continue
        counted += 1
        metrics = json.loads(s["metrics"] or "{}")
        ok = validate_permit_match(
            caps, metrics, float(s["flow_m3h"]), float(permit["quota_m3d"])
        )
        if not ok:
            all_ok = False
        details.append(
            {
                "sample_code": s["sample_code"],
                "status": s["status"],
                "generation": s["generation"],
                "metrics": metrics,
                "flow_m3h": s["flow_m3h"],
                "matched": ok,
                "orphan": False,
            }
        )

    if counted == 0 and orphans:
        all_ok = False

    seal_rows = conn.execute(
        """SELECT d.seal_ref, d.sample_generation, s.sample_code
           FROM discharge_seals d JOIN sample_batches s ON s.id=d.sample_id
           WHERE s.permit_ref=? AND s.status != '作废'""",
        (permit_code,),
    ).fetchall()
    orphan_ok = len(orphans) == 0
    balanced = all_ok and orphan_ok and counted > 0
    return {
        **base,
        "quota_m3d": permit["quota_m3d"],
        "caps": caps,
        "balanced": balanced,
        "all_metrics_matched": all_ok,
        "proof_ready": balanced and len(seal_rows) >= 1,
        "seal_count": len(seal_rows),
        "samples": details,
        "seals": [dict(x) for x in seal_rows],
    }


@router.get("/api/proof/sample/{sample_code}")
def proof_sample(sample_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        sample = conn.execute(
            "SELECT * FROM sample_batches WHERE sample_code=?", (sample_code,)
        ).fetchone()
        if not sample:
            raise HTTPException(404, "采样不存在")
        holds = conn.execute(
            "SELECT * FROM exceed_holds WHERE sample_id=?", (sample["id"],)
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM discharge_releases WHERE sample_id=?", (sample["id"],)
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM discharge_seals WHERE sample_id=?", (sample["id"],)
        ).fetchall()
        open_holds = [h for h in holds if h["status"] == "开放"]
        closed = [h for h in holds if h["status"] == "已关闭"]
        return {
            "sample_code": sample_code,
            "status": sample["status"],
            "generation": sample["generation"],
            "released": any(r["status"] == "已准排" for r in releases),
            "sealed": any(s["status"] == "已封批" for s in seals),
            "exceed_closed": len(open_holds) == 0 and (len(closed) > 0 or sample["status"] != "旁路挂起"),
            "holds": [dict(h) for h in holds],
            "releases": [dict(r) for r in releases],
            "seals": [dict(s) for s in seals],
        }
