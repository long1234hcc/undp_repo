"""
UNDP Meteorology — Forecast Fetch & Insert (Centroid-Based Architecture)
────────────────────────────────────────────────────────────────────────
Mục đích:
  1. Trích xuất tọa độ tâm (Centroid) của các quận Bangkok từ DB.
  2. Gọi Open-Meteo Forecast API lấy temperature_2m_max/min + relative_humidity_2m_max/min
     theo độ phân giải NGÀY (daily) cho hôm nay + 3 ngày tiếp theo.
  3. Tính temperature_2m_mean = (max + min) / 2
     Tính relative_humidity_2m_mean = (max + min) / 2
  4. Insert vào PostgreSQL (bảng weather_forecasts) kèm forecast_run_date.
  5. Không TRUNCATE — giữ lịch sử mỗi lần run để so sánh forecast vs actual.
────────────────────────────────────────────────────────────────────────
Schema bảng weather_forecasts:
  CREATE TABLE IF NOT EXISTS weather_forecasts (
      id                    SERIAL PRIMARY KEY,
      gid_1                 TEXT,
      gid_2                 TEXT,
      lat_center            NUMERIC,
      lon_center            NUMERIC,
      forecast_run_date     DATE,        -- Ngày script được chạy
      forecast_date         DATE,        -- Ngày được dự báo
      temperature_2m        NUMERIC,     -- Mean = (max + min) / 2
      relative_humidity_2m  NUMERIC,     -- Mean = (max + min) / 2
      UNIQUE (gid_2, forecast_date, forecast_run_date)
  );
────────────────────────────────────────────────────────────────────────
"""

import os
import time
import logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import date, timedelta

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5433)),
    "dbname":   os.getenv("DB_NAME", "undp_db"),
    "user":     os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "secretpassword"),
}

FORECAST_DAYS = 4          # Hôm nay + 3 ngày tiếp theo
BATCH_SIZE    = 30         # Số quận mỗi batch (giữ nguyên như code cũ)
REQUEST_DELAY = 40         # Giây nghỉ giữa các batch

API_URL = "https://api.open-meteo.com/v1/forecast"


# ═══════════════════════════════════════════════════════════════
# Bước 1: Lấy tọa độ Tâm (Centroid) của các Quận từ DB
# ═══════════════════════════════════════════════════════════════
def get_district_centroids(conn: psycopg2.extensions.connection) -> list[dict]:
    log.info("[DB] Đang trích xuất tọa độ tâm (Centroid) của các quận Bangkok...")
    points = []
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                gid_1,
                gid_2,
                district_name,
                province_name,
                ROUND(ST_Y(ST_Centroid(geom))::numeric, 8) AS lat_center,
                ROUND(ST_X(ST_Centroid(geom))::numeric, 8) AS lon_center
            FROM admin_polygons_district
            WHERE country_code = 'THA'
            AND province_name = 'BangkokMetropolis'
        """)
        rows = cur.fetchall()
        for row in rows:
            points.append({
                "gid_1":         row[0],
                "gid_2":         row[1],
                "district_name": row[2],
                "province_name": row[3],
                "lat_center":    float(row[4]),
                "lon_center":    float(row[5]),
            })

    log.info(f"[DB] Đã lấy thành công {len(points)} tâm quận/huyện.")
    return points


# ═══════════════════════════════════════════════════════════════
# Bước 2: Gọi Forecast API theo batch
# ═══════════════════════════════════════════════════════════════
def fetch_batch(batch_points: list[dict]) -> list[dict]:
    lats = ",".join(str(p["lat_center"]) for p in batch_points)
    lons = ",".join(str(p["lon_center"]) for p in batch_points)

    params = {
        "latitude":      lats,
        "longitude":     lons,
        "daily":         ",".join([
                             "temperature_2m_max",
                             "temperature_2m_min",
                             "relative_humidity_2m_max",
                             "relative_humidity_2m_min",
                         ]),
        "timezone":      "UTC",
        "forecast_days": FORECAST_DAYS,
    }

    resp = requests.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # API trả dict nếu chỉ 1 điểm, list nếu nhiều điểm
    if isinstance(data, dict):
        data = [data]

    return data


# ═══════════════════════════════════════════════════════════════
# Bước 3: Parse response → rows
# ═══════════════════════════════════════════════════════════════
def parse_response(
    api_response_list: list[dict],
    batch_points:      list[dict],
    run_date:          date,
) -> list[tuple]:
    rows: list[tuple] = []
    skipped = 0

    if len(api_response_list) != len(batch_points):
        log.warning(
            f"  [Parse] Mismatch: API trả {len(api_response_list)} objects "
            f"nhưng batch có {len(batch_points)} điểm — bỏ qua batch này"
        )
        return rows

    for point, obj in zip(batch_points, api_response_list):
        daily    = obj.get("daily", {})
        times    = daily.get("time", [])
        temp_max = daily.get("temperature_2m_max", [])
        temp_min = daily.get("temperature_2m_min", [])
        hum_max  = daily.get("relative_humidity_2m_max", [])
        hum_min  = daily.get("relative_humidity_2m_min", [])

        if not times:
            skipped += 1
            continue

        for t, t_max, t_min, h_max, h_min in zip(times, temp_max, temp_min, hum_max, hum_min):
            forecast_date = date.fromisoformat(t)

            # Tính mean = (max + min) / 2
            temp_mean = (
                round((float(t_max) + float(t_min)) / 2, 4)
                if t_max is not None and t_min is not None
                else None
            )
            hum_mean = (
                round((float(h_max) + float(h_min)) / 2, 4)
                if h_max is not None and h_min is not None
                else None
            )

            rows.append((
                point["gid_1"],
                point["gid_2"],
                point["lat_center"],
                point["lon_center"],
                run_date,       # forecast_run_date
                forecast_date,  # forecast_date
                temp_mean,      # temperature_2m
                hum_mean,       # relative_humidity_2m
            ))

    if skipped:
        log.warning(f"  [Parse] {skipped} point(s) không có daily data — bỏ qua")

    return rows


# ═══════════════════════════════════════════════════════════════
# Bước 4: Tạo bảng nếu chưa có + Insert vào PostgreSQL
# ═══════════════════════════════════════════════════════════════
def ensure_table(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_forecasts (
                id                    SERIAL PRIMARY KEY,
                gid_1                 TEXT,
                gid_2                 TEXT,
                lat_center            NUMERIC,
                lon_center            NUMERIC,
                forecast_run_date     DATE,
                forecast_date         DATE,
                temperature_2m        NUMERIC,
                relative_humidity_2m  NUMERIC,
                UNIQUE (gid_2, forecast_date, forecast_run_date)
            );
        """)
    conn.commit()
    log.info("[DB] Bảng weather_forecasts đã sẵn sàng.")


def insert_forecasts(
    conn: psycopg2.extensions.connection,
    rows: list[tuple],
) -> None:
    if not rows:
        return

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO weather_forecasts
                (gid_1, gid_2, lat_center, lon_center,
                 forecast_run_date, forecast_date,
                 temperature_2m, relative_humidity_2m)
            VALUES %s
            ON CONFLICT (gid_2, forecast_date, forecast_run_date) DO NOTHING
            """,
            rows,
            page_size=1000,
        )
    conn.commit()


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    run_date  = date.today()
    end_date  = run_date + timedelta(days=FORECAST_DAYS - 1)

    log.info("=" * 60)
    log.info("  UNDP Meteorology — Forecast Fetch & Insert")
    log.info(f"  Run date : {run_date}  (forecast_run_date)")
    log.info(f"  Forecast : {run_date} → {end_date}  ({FORECAST_DAYS} ngày)")
    log.info(f"  Model    : Open-Meteo Best Match  |  Resolution: Daily")
    log.info("=" * 60)

    # ── 1. Kết nối DB ────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công")

    # ── 2. Đảm bảo bảng tồn tại ──────────────────────────────
    ensure_table(conn)

    # ── 3. Lấy tọa độ từ DB ──────────────────────────────────
    target_points = get_district_centroids(conn)

    if not target_points:
        log.error("[!] Không tìm thấy tọa độ quận nào. Kiểm tra bảng admin_polygons_district.")
        conn.close()
        return

    n_batches = (len(target_points) + BATCH_SIZE - 1) // BATCH_SIZE
    log.info(
        f"[Info] {len(target_points)} quận × {FORECAST_DAYS} ngày "
        f"= {len(target_points) * FORECAST_DAYS:,} rows kỳ vọng"
    )
    log.info(f"[Fetch] {n_batches} batch × {BATCH_SIZE} điểm/batch — delay {REQUEST_DELAY}s/batch")
    log.info("-" * 60)

    # ── 4. Fetch + parse + insert theo batch ─────────────────
    total_rows   = 0
    failed_batch = 0

    for i in range(0, len(target_points), BATCH_SIZE):
        batch     = target_points[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        log.info(f"  Batch {batch_num:03d}/{n_batches} — {len(batch)} điểm ...")

        try:
            api_data = fetch_batch(batch)
            rows     = parse_response(api_data, batch, run_date)
            insert_forecasts(conn, rows)
            total_rows += len(rows)
            log.info(f"    ✓ {len(rows):,} rows inserted")

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "N/A"
            log.error(f"    ✗ HTTP {status} — skip batch {batch_num}")
            failed_batch += 1
            continue

        except requests.RequestException as e:
            log.error(f"    ✗ Request Error: {e}")
            failed_batch += 1
            continue

        except Exception as e:
            log.error(f"    ✗ Unexpected Error: {e}", exc_info=True)
            failed_batch += 1
            conn.rollback()
            continue

        finally:
            if i + BATCH_SIZE < len(target_points):  # Không sleep sau batch cuối
                time.sleep(REQUEST_DELAY)

    # ── 5. Summary ───────────────────────────────────────────
    log.info("-" * 60)
    log.info(f"[Done] Tổng rows inserted  : {total_rows:,}")
    log.info(f"       Batch thất bại      : {failed_batch}/{n_batches}")
    log.info(f"       Kỳ vọng (lý thuyết) : {len(target_points) * FORECAST_DAYS:,}")

    # ── 6. Verify DB ─────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM weather_forecasts")
        total_count: int = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM weather_forecasts
            WHERE forecast_run_date = %s
        """, (run_date,))
        today_count: int = cur.fetchone()[0]

        cur.execute("""
            SELECT MIN(forecast_date), MAX(forecast_date)
            FROM weather_forecasts
            WHERE forecast_run_date = %s
        """, (run_date,))
        row = cur.fetchone()
        min_fc, max_fc = row if row else (None, None)

        cur.execute("""
            SELECT COUNT(DISTINCT forecast_run_date) FROM weather_forecasts
        """)
        total_runs: int = cur.fetchone()[0]

    log.info("")
    log.info("[Verify DB]")
    log.info(f"  weather_forecasts (tổng)      : {total_count:,} rows")
    log.info(f"  Run hôm nay ({run_date})  : {today_count:,} rows")
    log.info(f"  Forecast range hôm nay        : {min_fc} → {max_fc}")
    log.info(f"  Tổng số lần run đã lưu        : {total_runs} run(s)")

    conn.close()
    log.info("[DB] Connection closed. Done!")


if __name__ == "__main__":
    main()