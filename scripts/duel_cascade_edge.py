#!/usr/bin/env python3
"""effluent-bugfix-04 争用脚本 duel_cascade_edge。

两路会话**同时**打「同一父边」的强制删除写口，验证血缘锁：
- 恰好一路 200（甲路径同事务级联作废全部子指标，cascaded=true）；
- 败方 409，业务零改，只在 contention_log 留争用痕。

两种打法：
1) 打活服务（验收段「并发双请求打争用写口」）：
     python scripts/duel_cascade_edge.py \
         --base-url http://127.0.0.1:11629 --edge 1
   可用 BF04_BASE_URL / BF04_EDGE 环境变量。
2) 不起服务，进程内 TestClient + 临时库真并发（默认）：
     python scripts/duel_cascade_edge.py --inproc

退出码 0 表示争用闸通过，1 表示发现「无锁双删」或不变量被破坏。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


# ------------------------------- HTTP 模式 -------------------------------- #

def _http_json(base: str, method: str, path: str, token: str | None = None):
    url = base.rstrip("/") + path
    data = b"{}" if method.upper() == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw}


def _login(base: str, user: str, password: str) -> str:
    payload = json.dumps({"username": user, "password": password}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/api/login", data=payload, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["token"]


def duel_http(base: str, edge: int, user_a: str, pw_a: str, user_b: str, pw_b: str) -> int:
    ta = _login(base, user_a, pw_a)
    tb = _login(base, user_b, pw_b)
    barrier = threading.Barrier(2)
    results: list[tuple[int, dict]] = []

    def strike(token: str):
        barrier.wait()  # 尽量同一瞬间发出，逼出血缘锁争用
        results.append(_http_json(base, "POST", f"/api/lineage/{edge}/force-delete", token))

    threads = [threading.Thread(target=strike, args=(ta,)),
               threading.Thread(target=strike, args=(tb,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    return _verdict(results, edge, base, ta)


def _verdict(results, edge: int, base: str | None, token: str | None = None) -> int:
    print("== duel_cascade_edge ==")
    for i, (status, body) in enumerate(results, 1):
        print(f"  会话{i}: HTTP {status} cascaded={body.get('cascaded')} "
              f"promoted={body.get('promoted')} invalidated={body.get('invalidated_sample_ids')} "
              f"detail={body.get('detail')}")
    statuses = sorted(s for s, _ in results)
    ok = statuses == [200, 409]
    winner = next((b for s, b in results if s == 200), {})
    ok = ok and winner.get("cascaded") is True

    if base:
        try:
            _, locks = _http_json(base, "GET", "/api/locks", token)
            traces = [c for c in locks.get("contention", []) if f"lineage-edge:{edge}" in (c.get("lock_key") or "")]
            print(f"  contention_log 血缘边争用痕: {len(traces)} 条")
            ok = ok and len(traces) >= 1
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 读取争用痕失败: {e}")

    if ok:
        print("  判定: PASS —— 一成一争，胜方同事务级联，败方 409 零改留痕。")
        return 0
    print("  判定: FAIL —— 出现无锁双删/双 200/缺少争用痕（BF04 争用未修复）。")
    return 1


# ------------------------------ InProc 模式 ------------------------------- #

def duel_inproc() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "backend"))

    import db  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    from app import app  # noqa: E402

    tmp = Path(tempfile.mkdtemp(prefix="duel_bf04_")) / "duel.db"
    db.DB_PATH = tmp
    db.init_db()

    def token(user: str, pw: str) -> str:
        c = TestClient(app)
        return c.post("/api/login", json={"username": user, "password": pw}).json()["token"]

    h1 = {"Authorization": "Bearer " + token("admin", "admin123")}
    h2 = {"Authorization": "Bearer " + token("admin", "admin123")}  # 同账号双会话也须互斥

    barrier = threading.Barrier(2)

    def strike(headers):
        c = TestClient(app)
        barrier.wait()
        r = c.post("/api/lineage/1/force-delete", headers=headers)
        try:
            body = r.json()
        except Exception:  # noqa: BLE001
            body = {}
        c.close()
        return r.status_code, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(strike, [h1, h2]))

    code = _verdict(results, 1, None)

    # 进程内再核库：边没了、子指标作废、审计仅一笔、争用痕至少一条。
    import sqlite3
    conn = sqlite3.connect(tmp)
    edge = conn.execute("SELECT COUNT(*) FROM metric_lineage WHERE id=1").fetchone()[0]
    child = conn.execute("SELECT status FROM sample_batches WHERE id=2").fetchone()[0]
    audits = conn.execute(
        "SELECT COUNT(*) FROM audit_trail WHERE action='lineage_force_delete' AND target='1'"
    ).fetchone()[0]
    traces = conn.execute(
        "SELECT COUNT(*) FROM contention_log WHERE lock_key='lineage-edge:1'"
    ).fetchone()[0]
    print(f"  核库: edge_rows={edge} child_status={child} force_audit={audits} contention={traces}")
    db_ok = edge == 0 and child == "作废" and audits == 1 and traces >= 1
    return 0 if code == 0 and db_ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="BF04 同边双会话删除争用脚本")
    p.add_argument("--base-url", default=os.environ.get("BF04_BASE_URL"))
    p.add_argument("--edge", type=int, default=int(os.environ.get("BF04_EDGE", "1")))
    p.add_argument("--user-a", default=os.environ.get("BF04_USER_A", "admin"))
    p.add_argument("--pw-a", default=os.environ.get("BF04_PW_A", "admin123"))
    p.add_argument("--user-b", default=os.environ.get("BF04_USER_B", "admin"))
    p.add_argument("--pw-b", default=os.environ.get("BF04_PW_B", "admin123"))
    p.add_argument("--inproc", action="store_true", help="不起活服务，进程内真并发")
    args = p.parse_args()

    if args.inproc or not args.base_url:
        return duel_inproc()
    return duel_http(
        args.base_url, args.edge,
        args.user_a, args.pw_a, args.user_b, args.pw_b,
    )


if __name__ == "__main__":
    raise SystemExit(main())
