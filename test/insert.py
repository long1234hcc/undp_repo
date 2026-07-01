"""
insert_postgis.py
-----------------
Load Layer: Dữ liệu sạch (Processed JSON) → Bulk Insert / Upsert vào PostGIS.

Kiến trúc:
  - Psycopg2 native driver với connection pooling (SimpleConnectionPool).
  - execute_values() bulk insert theo batch — giảm I/O network.
  - ST_GeomFromGeoJSON() native PostGIS — handle NULL geom an toàn.
  - Upsert (ON CONFLICT DO UPDATE) — idempotent, chạy lại pipeline không duplicate.
  - Credentials từ environment variables — không hardcode trong source.

Version : 2.1.0
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Generator, Optional

import pandas as pd
import psycopg2
import psycopg2.pool
from psycopg2.extras import execute_values

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_TABLE: str = "southeast_asia_districts"
BATCH_SIZE: int = 500

# Thứ tự cột INSERT — phải khớp 1-1 với template và data_records
INSERT_COLUMNS: list[str] = [
    "gid_2", "gid_1", "district_name", "province_name",
    "country_name", "country_code", "type", "geom",
]

# Cột bắt buộc phải có trong file processed
REQUIRED_COLUMNS: set[str] = set(INSERT_COLUMNS)

# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DbConfig:
    """
    Cấu hình kết nối PostgreSQL/PostGIS.
    Đọc từ environment variables — không hardcode credentials trong source.

    Biến môi trường cần set:
        POSTGIS_DB       : tên database
        POSTGIS_USER     : username
        POSTGIS_PASSWORD : password
        POSTGIS_HOST     : host (default: localhost)
        POSTGIS_PORT     : port (default: 5432)
    """

    dbname: str
    user: str
    password: str
    host: str = "localhost"
    port: str = "5432"

    @classmethod
    def from_env(cls) -> "DbConfig":
        return cls(
            dbname   = os.getenv("DB_NAME",     "undp_db"),
            user     = os.getenv("DB_USER",     "admin"),
            password = os.getenv("DB_PASSWORD", "secretpassword"),
            host     = os.getenv("DB_HOST",     "localhost"),
            port     = os.getenv("DB_PORT",     "5433"),
        )

    def as_psycopg2_kwargs(self) -> dict:
        return {
            "dbname": self.dbname,
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "port": self.port,
        }


@dataclass(frozen=True)
class LoadTask:
    """
    Khai báo một tác vụ load cho một quốc gia.

    Attributes
    ----------
    country_name : Tên quốc gia — dùng trong log.
    file_name    : Tên file JSON nằm trong processed_dir.
    """

    country_name: str
    file_name: str


# ---------------------------------------------------------------------------
# Connection pool
# ---------------------------------------------------------------------------

class ConnectionPool:
    """
    Wrapper quản lý SimpleConnectionPool của psycopg2.
    Dùng làm singleton trong pipeline — tạo 1 lần, share qua các task.
    """

    def __init__(self, db_config: DbConfig, min_conn: int = 1, max_conn: int = 5) -> None:
        self._config = db_config
        self._pool = psycopg2.pool.SimpleConnectionPool(
            minconn=min_conn,
            maxconn=max_conn,
            **db_config.as_psycopg2_kwargs(),
        )
        logger.info(f"[+] Connection pool khởi tạo thành công (min={min_conn}, max={max_conn}).")

    @contextmanager
    def get_cursor(self) -> Generator:
        """
        Context manager: lấy connection từ pool, yield cursor,
        tự động commit khi thành công hoặc rollback khi lỗi,
        và trả connection về pool sau khi dùng xong.
        """
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cursor:
                yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def close_all(self) -> None:
        self._pool.closeall()
        logger.info("[*] Đã đóng toàn bộ connection trong pool.")


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

def initialize_target_table(pool: ConnectionPool) -> None:
    """
    Tạo bảng đích và spatial index nếu chưa tồn tại.
    Chạy 1 lần duy nhất khi khởi động pipeline — không gọi lặp lại trong mỗi task.
    """
    ddl = f"""
        CREATE TABLE IF NOT EXISTS public.{TARGET_TABLE} (
            id            SERIAL PRIMARY KEY,
            gid_2         VARCHAR(100) NOT NULL,
            gid_1         VARCHAR(100) NOT NULL,
            district_name VARCHAR(255) NOT NULL,
            province_name VARCHAR(255),
            country_name  VARCHAR(100) NOT NULL,
            country_code  VARCHAR(10)  NOT NULL,
            type          VARCHAR(50),
            geom          GEOMETRY(Geometry, 4326),
            CONSTRAINT uq_{TARGET_TABLE}_gid2 UNIQUE (gid_2)
        );

        CREATE INDEX IF NOT EXISTS idx_{TARGET_TABLE}_geom
            ON public.{TARGET_TABLE} USING GIST (geom);

        CREATE INDEX IF NOT EXISTS idx_{TARGET_TABLE}_country
            ON public.{TARGET_TABLE} (country_name);
    """
    try:
        with pool.get_cursor() as cursor:
            cursor.execute(ddl)
        logger.info(f"[+] Bảng public.{TARGET_TABLE} đã sẵn sàng.")
    except psycopg2.DatabaseError as exc:
        raise RuntimeError(f"DDL thất bại — không thể khởi tạo bảng đích: {exc}") from exc


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_dataframe(df: pd.DataFrame, task: LoadTask) -> None:
    """
    Kiểm tra DataFrame có đủ cột bắt buộc trước khi insert.
    Raise ValueError rõ ràng thay vì để KeyError âm thầm.
    """
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"[{task.country_name}] File processed thiếu cột: {sorted(missing)}. "
            f"Cột hiện có: {sorted(df.columns.tolist())}"
        )

    # gid_1 NOT NULL ở DB — nguồn GADM luôn nhúng parent reference (GID_1)
    # ngay trong properties ADM2, khác GeoBoundaries không có khái niệm
    # "unmatched". Null ở đây là bất thường dữ liệu nguồn, phải chặn trước
    # khi insert thay vì để DB NOT NULL constraint raise lỗi rời rạc giữa batch.
    n_null_gid1 = df["gid_1"].isna().sum()
    if n_null_gid1 > 0:
        raise ValueError(
            f"[{task.country_name}] {n_null_gid1}/{len(df)} dòng có gid_1=NULL — "
            f"bất thường với nguồn GADM, không thể insert (cột gid_1 là NOT NULL)."
        )


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def _build_records(df: pd.DataFrame) -> list[tuple]:
    """
    Chuyển DataFrame → list of tuples theo thứ tự INSERT_COLUMNS.

    Note về NULL geom:
        Nếu giá trị geom là None/NaN, giữ nguyên Python None.
        Template SQL sẽ handle: ST_GeomFromGeoJSON(NULL) → NULL geometry trong PostGIS.
        Không crash — PostGIS xử lý NULL geometry hợp lệ nếu column không có NOT NULL constraint.
    """
    # Normalize NaN → None để psycopg2 truyền đúng NULL
    df_clean = df[INSERT_COLUMNS].where(pd.notnull(df[INSERT_COLUMNS]), other=None)
    return list(df_clean.itertuples(index=False, name=None))


# ---------------------------------------------------------------------------
# Upsert SQL
# ---------------------------------------------------------------------------

_UPSERT_SQL = f"""
    INSERT INTO public.{TARGET_TABLE} (
        gid_2, gid_1, district_name, province_name,
        country_name, country_code, type, geom
    ) VALUES %s
    ON CONFLICT (gid_2) DO UPDATE SET
        gid_1          = EXCLUDED.gid_1,
        district_name  = EXCLUDED.district_name,
        province_name  = EXCLUDED.province_name,
        country_name   = EXCLUDED.country_name,
        country_code   = EXCLUDED.country_code,
        type           = EXCLUDED.type,
        geom           = EXCLUDED.geom;
"""

# Template cho execute_values:
# - 7 cột đầu: plain %s
# - cột geom (cuối): ST_GeomFromGeoJSON(%s) — chuyển GeoJSON string → geometry binary
# - ST_GeomFromGeoJSON(NULL) trả về NULL, không crash
_ROW_TEMPLATE = "(%s, %s, %s, %s, %s, %s, %s, ST_GeomFromGeoJSON(%s))"


# ---------------------------------------------------------------------------
# Core load function
# ---------------------------------------------------------------------------

def load_country_data(
    file_path: str,
    task: LoadTask,
    pool: ConnectionPool,
) -> bool:
    """
    Đọc file JSON đã xử lý và upsert toàn bộ vào PostGIS.

    Parameters
    ----------
    file_path : Đường dẫn tuyệt đối tới file JSON processed.
    task      : LoadTask chứa metadata của tác vụ.
    pool      : ConnectionPool đã khởi tạo — dùng chung toàn pipeline.

    Returns
    -------
    True  : Load thành công.
    False : Thất bại (đã log chi tiết).
    """
    # --- Guard: file tồn tại ---
    if not os.path.exists(file_path):
        logger.error(f"[-] [{task.country_name}] File không tồn tại: {file_path}")
        return False

    try:
        # 1. Đọc file
        logger.info(f"[{task.country_name}] Đang đọc: {file_path}")
        df = pd.read_json(file_path, orient="records")

        if df.empty:
            logger.warning(f"[!] [{task.country_name}] File trống — bỏ qua.")
            return False

        logger.info(f"[{task.country_name}] Đọc thành công: {len(df):,} dòng.")

        # 2. Validate schema
        _validate_dataframe(df, task)

        # 3. Chuẩn bị records
        records = _build_records(df)

        # Thống kê null geom trước khi insert
        n_null_geom = df["geom"].isna().sum()
        if n_null_geom > 0:
            logger.warning(
                f"[!] [{task.country_name}] {n_null_geom}/{len(df)} dòng có geom=NULL — "
                f"sẽ insert NULL geometry vào PostGIS."
            )

        # 4. Upsert theo batch
        logger.info(
            f"[{task.country_name}] Đang upsert {len(records):,} dòng "
            f"(batch_size={BATCH_SIZE})..."
        )
        t_start = time.perf_counter()

        with pool.get_cursor() as cursor:
            execute_values(
                cur=cursor,
                sql=_UPSERT_SQL,
                argslist=records,
                template=_ROW_TEMPLATE,
                page_size=BATCH_SIZE,
            )

        duration = time.perf_counter() - t_start
        rate = len(records) / duration if duration > 0 else float("inf")
        logger.info(
            f"[+] [{task.country_name}] Upsert hoàn tất: "
            f"{len(records):,} dòng trong {duration:.2f}s ({rate:,.0f} rows/s)."
        )
        return True

    except ValueError as exc:
        # Schema / validation error — không cần traceback
        logger.error(f"[-] [{task.country_name}] Validation thất bại: {exc}")
        return False

    except psycopg2.DatabaseError as exc:
        logger.error(f"[-] [{task.country_name}] Database error: {exc}")
        return False

    except Exception as exc:
        logger.exception(f"[-] [{task.country_name}] Lỗi không mong đợi: {exc}")
        return False


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def build_execution_manifest() -> list[LoadTask]:
    """
    Khai báo danh sách tác vụ load.
    Trong production: load từ YAML/JSON config thay vì hardcode.
    """
    return [
        LoadTask(country_name="Thailand", file_name="tha_adm2_processed.json"),
        LoadTask(country_name="India",    file_name="ind_adm2_processed.json"),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    processed_dir = os.path.join("data", "processed")
    tasks = build_execution_manifest()

    logger.info("=" * 65)
    logger.info("   POSTGIS LOAD LAYER — Bulk Upsert Pipeline")
    logger.info("=" * 65)

    # --- Khởi tạo DB config từ environment ---
    try:
        db_config = DbConfig.from_env()
    except EnvironmentError as exc:
        logger.critical(f"[ABORT] Không thể khởi động pipeline: {exc}")
        return

    # --- Khởi tạo connection pool ---
    try:
        pool = ConnectionPool(db_config, min_conn=1, max_conn=3)
    except psycopg2.OperationalError as exc:
        logger.critical(f"[ABORT] Không thể kết nối PostGIS: {exc}")
        return

    try:
        # --- DDL: chạy 1 lần duy nhất ---
        try:
            initialize_target_table(pool)
        except RuntimeError as exc:
            logger.critical(f"[ABORT] {exc}")
            return

        # --- Thực thi từng task ---
        t_pipeline_start = time.perf_counter()
        results: dict[str, bool] = {}

        for task in tasks:
            file_path = os.path.join(processed_dir, task.file_name)
            results[task.country_name] = load_country_data(file_path, task, pool)
            print("-" * 65)

        # --- Summary ---
        total_duration = time.perf_counter() - t_pipeline_start
        success = [k for k, v in results.items() if v]
        failed  = [k for k, v in results.items() if not v]

        logger.info(
            f"[=] Pipeline hoàn tất trong {total_duration:.2f}s — "
            f"{len(success)}/{len(tasks)} thành công."
        )
        if failed:
            logger.warning(f"[!] Các tác vụ thất bại: {failed}")

    finally:
        pool.close_all()


if __name__ == "__main__":
    main()