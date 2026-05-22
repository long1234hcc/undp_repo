"""
UNDP Meteorology — Population Import Script
════════════════════════════════════════════════════════════════════════
Script độc lập: đọc file JSON population → tạo bảng pop_district → insert.

Schema: pop_district
  district_code   TEXT PRIMARY KEY   -- Khớp với gid_2 trong admin_polygons_district
  district_name   TEXT
  population      NUMERIC
  density         NUMERIC            -- Mật độ dân số (người/km²)

Usage:
  python import_population.py --file population.json
  python import_population.py --file population.json --truncate   # truncate trước khi insert

════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import logging
import argparse
import psycopg2
from psycopg2.extras import execute_values

# ── Logging ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── DB Config ────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", 5433)),
    "dbname":   os.getenv("DB_NAME",     "undp_db"),
    "user":     os.getenv("DB_USER",     "admin"),
    "password": os.getenv("DB_PASSWORD", "secretpassword"),
}


# ════════════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════════════

def ensure_table(conn: psycopg2.extensions.connection) -> None:
    """Tạo bảng pop_district nếu chưa tồn tại."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pop_district (
                district_code   TEXT PRIMARY KEY,
                district_name   TEXT,
                population      NUMERIC,
                density         NUMERIC
            );
        """)
    conn.commit()
    log.info("[DB] Bảng pop_district đã sẵn sàng.")


def truncate_table(conn: psycopg2.extensions.connection) -> None:
    """Xóa toàn bộ data trong pop_district."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE pop_district;")
    conn.commit()
    log.info("[DB] Truncate pop_district hoàn tất.")


def insert_population(
    conn: psycopg2.extensions.connection,
    rows: list[tuple],
) -> int:
    """
    Insert population rows vào pop_district.
    ON CONFLICT (district_code) DO UPDATE — idempotent, chạy lại không lỗi.
    Trả về số rows affected.
    """
    if not rows:
        log.warning("[DB] Không có rows để insert.")
        return 0

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO pop_district (district_code, district_name, population, density)
            VALUES %s
            ON CONFLICT (district_code) DO UPDATE SET
                district_name = EXCLUDED.district_name,
                population    = EXCLUDED.population,
                density       = EXCLUDED.density
            """,
            rows,
            page_size=500,
        )
        affected = cur.rowcount
    conn.commit()
    return affected


# ════════════════════════════════════════════════════════════════════
# PARSE JSON
# ════════════════════════════════════════════════════════════════════

def load_json(filepath: str) -> list[dict]:
    """Đọc và validate file JSON population."""
    if not os.path.exists(filepath):
        log.error(f"[Load] File không tồn tại: {filepath}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        log.error("[Load] File JSON phải là array ở root level.")
        sys.exit(1)

    log.info(f"[Load] Đọc {len(data):,} records từ {filepath}")
    return data


def parse_records(data: list[dict]) -> list[tuple]:
    """
    Validate và convert list dict → list tuple sẵn sàng insert.
    Bỏ qua các record thiếu field bắt buộc, log warning.
    """
    REQUIRED_FIELDS = {"district_code", "district_name", "population", "density"}
    rows   = []
    n_skip = 0

    for i, record in enumerate(data):
        missing = REQUIRED_FIELDS - record.keys()
        if missing:
            log.warning(f"[Parse] Record #{i} thiếu field {missing} — bỏ qua.")
            n_skip += 1
            continue

        try:
            rows.append((
                str(record["district_code"]).strip(),
                str(record["district_name"]).strip(),
                float(record["population"]),
                float(record["density"]),
            ))
        except (ValueError, TypeError) as e:
            log.warning(f"[Parse] Record #{i} (code={record.get('district_code')}) lỗi type: {e} — bỏ qua.")
            n_skip += 1

    log.info(f"[Parse] {len(rows):,} records hợp lệ | {n_skip} records bị bỏ qua.")
    return rows


# ════════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import population JSON → PostgreSQL pop_district table"
    )
    parser.add_argument(
        "--file", "-f",
        required=True,
        help="Đường dẫn tới file JSON population (vd: population.json)",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        default=False,
        help="Truncate bảng trước khi insert (mặc định: upsert)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    log.info("=" * 60)
    log.info("  UNDP Meteorology — Population Import")
    log.info(f"  File     : {args.file}")
    log.info(f"  Mode     : {'TRUNCATE + INSERT' if args.truncate else 'UPSERT'}")
    log.info("=" * 60)

    # ── Load & parse JSON ────────────────────────────────────────
    raw_data = load_json(args.file)
    rows     = parse_records(raw_data)

    if not rows:
        log.error("[Main] Không có data hợp lệ để import. Dừng.")
        sys.exit(1)

    # ── Kết nối DB ──────────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} ...")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công.")

    try:
        ensure_table(conn)

        if args.truncate:
            truncate_table(conn)

        affected = insert_population(conn, rows)

        log.info("")
        log.info("=" * 60)
        log.info("  IMPORT HOÀN TẤT")
        log.info(f"  Records parsed   : {len(rows):,}")
        log.info(f"  Rows affected    : {affected:,}")
        log.info(f"  Mode             : {'TRUNCATE + INSERT' if args.truncate else 'UPSERT'}")
        log.info("=" * 60)

    except Exception as e:
        log.error(f"[Main] Lỗi nghiêm trọng: {e}", exc_info=True)
        conn.rollback()
        raise

    finally:
        conn.close()
        log.info("[DB] Connection closed.")


if __name__ == "__main__":
    main()