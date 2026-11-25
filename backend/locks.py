"""排放批争用锁。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit

router = APIRouter(tags=["locks"])


def _write_contention(conn, lock_key: str, holder: str, detail: str) -> None:
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用失败", detail, now_iso()),
    )


def record_contention(lock_key: str, actor: str, detail: str, action: str = "争用失败") -> None:
    """独立连接落争用痕并立即提交，保证败方响应 409 时痕迹不随业务事务回滚。"""
    conn = connect()
    try:
        conn.execute(
            """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
               VALUES (?,?,?,?,?)""",
            (lock_key, actor, action, detail, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def acquire_mutex(conn, lock_key: str, holder: str) -> bool:
    """纯互斥原子抢锁：锁键已存在（哪怕同名持有人）即失败。

    用 INSERT OR IGNORE + rowcount 消除旧 require_lock 的 SELECT-then-INSERT
    竞态，供「同会话双路删同一边也必须互斥」的血缘写口使用。
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
        (lock_key, holder, now_iso()),
    )
    return cur.rowcount == 1


def require_lock(conn, lock_key: str, holder: str) -> None:
    row = conn.execute(
        "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] != holder:
        _write_contention(conn, lock_key, holder, f"持有人={row['holder']}")
        conn.commit()
        raise HTTPException(409, f"锁被占用：{lock_key} 持有人 {row['holder']}")
    if not row:
        cur = conn.execute(
            "INSERT OR IGNORE INTO effluent_locks(lock_key,holder,acquired_at) VALUES (?,?,?)",
            (lock_key, holder, now_iso()),
        )
        if cur.rowcount == 0:
            winner = conn.execute(
                "SELECT holder FROM effluent_locks WHERE lock_key=?", (lock_key,)
            ).fetchone()
            who = winner["holder"] if winner else "未知"
            _write_contention(conn, lock_key, holder, f"并发抢锁失败，持有人={who}")
            conn.commit()
            raise HTTPException(409, f"锁被占用：{lock_key} 持有人 {who}")


def release(conn, lock_key: str, holder: str) -> None:
    conn.execute(
        "DELETE FROM effluent_locks WHERE lock_key=? AND holder=?",
        (lock_key, holder),
    )


@router.get("/api/locks")
def list_locks(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        locks = [dict(r) for r in conn.execute("SELECT * FROM effluent_locks").fetchall()]
        logs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM contention_log ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]
        return {"locks": locks, "contention": logs}


@router.post("/api/locks/acquire")
def acquire(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        require_lock(conn, lock_key, user)
        write_audit(conn, user, "lock_acquire", lock_key, {})
        conn.commit()
        return {"ok": True, "lock_key": lock_key, "holder": user}


@router.post("/api/locks/release")
def release_api(lock_key: str, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        release(conn, lock_key, user)
        write_audit(conn, user, "lock_release", lock_key, {})
        conn.commit()
        return {"ok": True}
