"""园区公共用电逐小时净电量 API。

- POST /api/hierarchy            上传整版电表层级 CSV（上级表, 下级表），整版替换
- POST /api/readings             按批补传累计电量 CSV（表号, UTC整点, kWh读数）
- GET  /api/meters/{id}/daily-net?date=YYYY-MM-DD   查询某 UTC 日逐小时净电量
- GET  /api/hierarchy            查看当前已生效层级（便于演示核对）
- GET  /api/batches              查看批次审计（含被拒批次及原因）
- GET  /api/health

一致性约定：每个请求一个事务；校验全部通过后才落库，任一步失败即回滚，
已提交的旧层级/旧读数/旧查询结果不受影响。
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel

from . import core, db, ingest
from .parsing import CsvError, parse_hierarchy_csv

DEFAULT_DB_PATH = os.environ.get(
    "ENERGY_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "energy.db")
)


# ---------- 响应模型（让 /docs 自描述） ----------

class BatchResult(BaseModel):
    batch_id: str
    status: str
    kind: str
    rows_applied: int
    message: str


class HourResult(BaseModel):
    hour_start: str
    status: str  # "ok" | "pending_review"
    net_kwh: Optional[float] = None
    master_delta_kwh: Optional[float] = None
    children_delta_kwh: Optional[float] = None
    review_reason: Optional[str] = None


class DailyNetResult(BaseModel):
    meter_id: str
    date: str
    child_meters: list[str]
    hours: list[HourResult]


class HierarchyEdge(BaseModel):
    parent_meter: str
    child_meter: str


class BatchRecord(BaseModel):
    batch_id: str
    kind: str
    status: str
    reason: Optional[str]
    row_count: int
    created_at: str


# ---------- 应用工厂 ----------

def create_app(db_path: Optional[str] = None) -> FastAPI:
    app = FastAPI(
        title="园区公共用电逐小时净电量 API",
        description=(
            "上传整版电表层级与按批补传累计电量，查询指定总表某 UTC 日的逐小时净电量。"
            "每小时 = 总表相邻整点差值 - 直接下级表差值之和（孙表不再扣）。"
        ),
        version="1.0.0",
    )
    app.state.db_path = os.path.abspath(db_path or DEFAULT_DB_PATH)
    app.state.staging_dir = ingest.default_staging_dir(app.state.db_path)
    ingest.sweep_stale(app.state.staging_dir)
    db.init_db(app.state.db_path)

    def get_conn(request: Request):
        conn = db.connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _record_rejected(conn, kind: str, reason: str) -> None:
        """拒收批次留痕。独立短事务，不影响主数据；留痕失败不掩盖原始错误。"""
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, ?, 'rejected', ?, 0, ?)",
                (str(uuid.uuid4()), kind, reason, _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
            except Exception:
                pass

    # ---------- 上传：整版层级 ----------

    @app.post("/api/hierarchy", response_model=BatchResult, summary="上传整版电表层级 CSV（整版替换）")
    async def upload_hierarchy(file: UploadFile = File(...), conn=Depends(get_conn)):
        text = await _read_csv_text(file)
        try:
            edges = parse_hierarchy_csv(text)  # 纯函数校验：成环/多上级/自环在此拒收
        except CsvError as exc:
            _record_rejected(conn, "hierarchy", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        batch_id = str(uuid.uuid4())
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM hierarchy")  # 整版替换
            conn.executemany(
                "INSERT INTO hierarchy (child_meter, parent_meter) VALUES (?, ?)",
                [(child, parent) for parent, child in edges],
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'hierarchy', 'applied', NULL, ?, ?)",
                (batch_id, len(edges), _utcnow()),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return BatchResult(
            batch_id=batch_id, status="applied", kind="hierarchy",
            rows_applied=len(edges),
            message=f"层级已整版替换，生效 {len(edges)} 条上下级关系",
        )

    # ---------- 上传：一批读数 ----------

    @app.post("/api/readings", response_model=BatchResult, summary="按批补传累计电量 CSV")
    def upload_readings(
        request: Request, file: UploadFile = File(...), conn=Depends(get_conn)
    ):
        # 同步端点：FastAPI 把它放进线程池，长时间解析不会阻塞其它请求的
        # 每小时查询；查询与落库各自用自己的连接和事务。
        batch_id = str(uuid.uuid4())
        now = _utcnow()
        try:
            file.file.seek(0)
            # 阶段一：流式解析 -> 主库同盘临时 SQLite（内存占用有界，整批查同键异值）
            # 阶段二：单事务合并进主表；此前任何失败都不触碰主表
            with ingest.StagingArea(request.app.state.staging_dir) as stage:
                ingest.stage_readings(stage, file.file)
                # 批内同键异值（含相隔很远的同键）在此整批拒收，同时得到键数
                row_count = ingest.count_distinct_keys(stage)
                ingest.merge_into_main(
                    conn, stage, batch_id=batch_id, now=now, row_count=row_count
                )
        except CsvError as exc:
            _record_rejected(conn, "readings", str(exc))
            raise HTTPException(status_code=422, detail=str(exc))
        except (sqlite3.Error, OSError) as exc:
            # 暂存/落库阶段的 I/O 失败：主表事务已回滚，不留下半批读数。
            _record_rejected(conn, "readings", f"写入失败: {exc}")
            raise HTTPException(status_code=500, detail=f"读数批次写入失败: {exc}")

        return BatchResult(
            batch_id=batch_id, status="applied", kind="readings",
            rows_applied=row_count,
            message=f"读数批次已提交，{row_count} 个（表号, 整点）键已写入/更正",
        )

    # ---------- 查询：逐小时净电量 ----------

    @app.get(
        "/api/meters/{meter_id}/daily-net",
        response_model=DailyNetResult,
        summary="查询指定总表某 UTC 日的逐小时净电量",
    )
    def daily_net(
        meter_id: str,
        date_: str = Query(..., alias="date", description="UTC 日期，格式 YYYY-MM-DD"),
        conn=Depends(get_conn),
    ):
        try:
            day = date.fromisoformat(date_)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"日期格式应为 YYYY-MM-DD: {date_!r}")

        # 单事务快照：层级与读数取自同一已提交状态
        conn.execute("BEGIN")
        try:
            children = [
                r["child_meter"]
                for r in conn.execute(
                    "SELECT child_meter FROM hierarchy WHERE parent_meter = ?"
                    " ORDER BY child_meter",
                    (meter_id,),
                )
            ]
            participants = [meter_id, *children]
            placeholders = ",".join("?" for _ in participants)
            t_start, t_end = core.hour_str(day, 0), core.hour_str(day, 24)
            rows = conn.execute(
                f"SELECT meter_id, ts_utc, kwh FROM readings"
                f" WHERE meter_id IN ({placeholders}) AND ts_utc BETWEEN ? AND ?",
                (*participants, t_start, t_end),
            ).fetchall()
            readings = {(r["meter_id"], r["ts_utc"]): r["kwh"] for r in rows}
            hours = core.compute_daily_net(meter_id, day, children, readings)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return DailyNetResult(
            meter_id=meter_id, date=day.isoformat(), child_meters=children, hours=hours
        )

    # ---------- 辅助端点 ----------

    @app.get("/api/hierarchy", response_model=list[HierarchyEdge], summary="查看当前已生效层级")
    def get_hierarchy(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT parent_meter, child_meter FROM hierarchy"
                " ORDER BY parent_meter, child_meter"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [HierarchyEdge(**dict(r)) for r in rows]

    @app.get("/api/batches", response_model=list[BatchRecord], summary="批次审计（含被拒批次）")
    def list_batches(conn=Depends(get_conn)):
        conn.execute("BEGIN")
        try:
            rows = conn.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return [BatchRecord(**dict(r)) for r in rows]

    @app.get("/api/health", summary="健康检查")
    def health():
        return {"status": "ok"}

    return app


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _read_csv_text(file: UploadFile) -> str:
    raw = await file.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=422, detail="CSV 必须是 UTF-8 编码")


app = create_app()
