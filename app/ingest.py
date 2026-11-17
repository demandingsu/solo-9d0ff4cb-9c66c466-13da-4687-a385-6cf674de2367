"""读数批次的磁盘暂存与原子落库。

24 万行级别的批次不能把 CSV 全文、逐行键集合或待写列表留在内存里。这里：

1. 边解析边把合法的 (表号, 整点, 读数, 行号) 写入一个**独立的临时 SQLite 文件**，
   内存占用只与固定的插入块大小有关；
2. 解析/校验全部通过后，才把暂存库 ATTACH 到主连接，在一个
   ``BEGIN IMMEDIATE`` 事务里用 ``INSERT ... SELECT`` 一次性合并进主表；
3. 任何失败（尾部坏行、同键异值、写入失败）都不触碰主表；落库阶段失败则回滚，
   因此不会留下部分生效的读数。

同键异值必须跨行比较，交由暂存库的 SQL 完成——相隔多远的同键都会被查出来。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from typing import IO

from .parsing import CsvError, iter_decoded_lines, iter_readings_csv

# 每次 executemany 的行数：限制 Python 侧待写列表的内存上限。
INSERT_CHUNK = 2_000

# 暂存库是纯临时数据，崩溃即整批作废，无需 WAL/同步落盘。
_STAGE_PRAGMAS = (
    "PRAGMA journal_mode = OFF",
    "PRAGMA synchronous = OFF",
    "PRAGMA temp_store = FILE",
    "PRAGMA cache_size = -8192",  # 每个连接约 8MB 页缓存，避免默认值外放大
)

_STAGE_SCHEMA = """
CREATE TABLE reading_rows (
    meter_id TEXT NOT NULL,
    ts_utc   TEXT NOT NULL,
    kwh      REAL NOT NULL,
    lineno   INTEGER NOT NULL
);
"""

_INSERT_ROW = (
    "INSERT INTO reading_rows (meter_id, ts_utc, kwh, lineno) VALUES (?, ?, ?, ?)"
)


# 暂存库所在目录：默认放在主库同目录（Docker 里是持久化磁盘卷），避免 /tmp
# 是 tmpfs 时把 24 万行暂存数据算进受限内存。可用 ENERGY_STAGING_DIR 覆盖。
STAGING_DIR = os.environ.get("ENERGY_STAGING_DIR")


def default_staging_dir(main_db_path: str) -> str:
    """主库旁边的 staging 子目录：与主库同在磁盘卷上。"""
    if STAGING_DIR:
        return STAGING_DIR
    return os.path.join(os.path.dirname(os.path.abspath(main_db_path)), "staging")


def sweep_stale(staging_dir: str, max_age_seconds: int = 24 * 3600) -> None:
    """清理进程崩溃残留的暂存目录（只动足够旧的，避免误删并发实例的批次）。"""
    cutoff = time.time() - max_age_seconds
    try:
        entries = os.scandir(staging_dir)
    except OSError:
        return
    with entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False) and entry.stat(
                    follow_symlinks=False
                ).st_mtime < cutoff:
                    shutil.rmtree(entry.path, ignore_errors=True)
            except OSError:
                continue


class StagingArea:
    """一个批次的临时暂存库，退出时关闭并删除临时目录。"""

    def __init__(self, staging_dir: str | None = None) -> None:
        if staging_dir:
            os.makedirs(staging_dir, exist_ok=True)
        self.dir_path = tempfile.mkdtemp(prefix="energy-readings-", dir=staging_dir)
        self.path = os.path.join(self.dir_path, "staging.db")
        self.conn: sqlite3.Connection | None = None
        try:
            self.conn = sqlite3.connect(self.path, isolation_level=None)
            for pragma in _STAGE_PRAGMAS:
                self.conn.execute(pragma)
            self.conn.executescript(_STAGE_SCHEMA)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
        except sqlite3.Error:
            pass
        finally:
            shutil.rmtree(self.dir_path, ignore_errors=True)

    def __enter__(self) -> "StagingArea":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def stage_readings(stage: "StagingArea", file_obj: IO[bytes]) -> int:
    """流式解析上传文件并写入暂存库，返回（未去重的）合法行数。

    这里只做单行校验；批内同键异值由 :func:`count_distinct_keys` 在暂存库中查。
    """
    conn = stage.conn
    assert conn is not None
    total = 0
    try:
        conn.execute("BEGIN")
        chunk: list[tuple[str, str, float, int]] = []
        for row in iter_readings_csv(iter_decoded_lines(file_obj)):
            chunk.append(row)
            if len(chunk) >= INSERT_CHUNK:
                conn.executemany(_INSERT_ROW, chunk)
                total += len(chunk)
                chunk.clear()
        if chunk:
            conn.executemany(_INSERT_ROW, chunk)
            total += len(chunk)
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    if total == 0:
        raise CsvError("CSV 没有任何读数数据行")
    return total


def count_distinct_keys(stage: StagingArea) -> int:
    """暂存库中的唯一键数（同键同值已按键合并）。

    顺带检查批内同键异值（相隔多远的同键都能查出），有异值即整批拒收。
    """
    conn = stage.conn
    assert conn is not None
    distinct_keys, conflicts = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(lo <> hi), 0)
        FROM (
            SELECT meter_id, ts_utc, MIN(kwh) AS lo, MAX(kwh) AS hi
            FROM reading_rows
            GROUP BY meter_id, ts_utc
        )
        """
    ).fetchone()
    if conflicts:
        raise CsvError(_describe_conflict(conn))
    return distinct_keys


def _describe_conflict(conn: sqlite3.Connection) -> str:
    """定位第一处同键异值，给出与内存版一致的可读原因。"""
    meter, ts = conn.execute(
        """
        SELECT meter_id, ts_utc
        FROM reading_rows
        GROUP BY meter_id, ts_utc
        HAVING MIN(kwh) <> MAX(kwh)
        LIMIT 1
        """
    ).fetchone()
    first_lineno, first_kwh = conn.execute(
        "SELECT lineno, kwh FROM reading_rows"
        " WHERE meter_id = ? AND ts_utc = ? ORDER BY lineno LIMIT 1",
        (meter, ts),
    ).fetchone()
    other_lineno, other_kwh = conn.execute(
        "SELECT lineno, kwh FROM reading_rows"
        " WHERE meter_id = ? AND ts_utc = ? AND kwh <> ? ORDER BY lineno LIMIT 1",
        (meter, ts, first_kwh),
    ).fetchone()
    return (
        f"一批内同键异值: 表 {meter!r} 在 {ts} 第 {first_lineno} 行为 "
        f"{first_kwh}，第 {other_lineno} 行为 {other_kwh}，整批拒收"
    )


def merge_into_main(
    conn: sqlite3.Connection,
    stage: StagingArea,
    *,
    batch_id: str,
    now: str,
    row_count: int,
) -> None:
    """在单个写事务内把暂存读数并入主表，并写入批次审计。

    同键异值 = 更正（覆盖）；同值重传 = 幂等（WHERE 保证 kwh 与 updated_at
    都不变）。任一步失败整体回滚，主表保持上传前的已提交状态。
    """
    conn.execute("ATTACH DATABASE ? AS reading_stage", (stage.path,))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO readings (meter_id, ts_utc, kwh, updated_at)
                SELECT meter_id, ts_utc, MIN(kwh), ?
                FROM reading_stage.reading_rows
                WHERE 1  -- UPSERT 与 SELECT 连用时需显式 WHERE，避免解析歧义
                GROUP BY meter_id, ts_utc
                ON CONFLICT (meter_id, ts_utc) DO UPDATE SET
                    kwh = excluded.kwh, updated_at = excluded.updated_at
                WHERE readings.kwh <> excluded.kwh
                """,
                (now,),
            )
            conn.execute(
                "INSERT INTO batches (batch_id, kind, status, reason, row_count, created_at)"
                " VALUES (?, 'readings', 'applied', NULL, ?, ?)",
                (batch_id, row_count, now),
            )
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.execute("DETACH DATABASE reading_stage")
        except sqlite3.Error:
            pass  # 连接即将关闭；临时文件由 StagingArea 删除
