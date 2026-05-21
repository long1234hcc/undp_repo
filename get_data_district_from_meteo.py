"""
UNDP Meteorology — Fetch & Insert (Centroid-Based Architecture)
────────────────────────────────────────────────────────────────
Mục đích:
  1. Trích xuất tọa độ tâm (Centroid) của ~928 quận/huyện (gid_2) tại Thái Lan từ DB.
  2. Gọi Open-Meteo Historical API lấy temperature_2m_mean + relative_humidity_2m_mean
     theo độ phân giải NGÀY (daily) cho trọn năm 2025.
  3. Parse response và map vào các cột hiện có của DB (temperature_2m, relative_humidity_2m).
  4. Insert thẳng vào PostgreSQL (bảng weather_observations) kèm sẵn gid_1, gid_2.
  5. Chuẩn hóa dữ liệu (Min-Max Normalization).
────────────────────────────────────────────────────────────────
"""

import os
import time
import logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, date

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

# Khoảng thời gian lấy data
START_DATE = "2025-01-01"
END_DATE   = "2025-12-31"

BATCH_SIZE = 30
HOURLY_CALL_LIMIT = 4800
REQUEST_DELAY = 40

API_URL = "https://archive-api.open-meteo.com/v1/archive"


# ═══════════════════════════════════════════════════════════════
# Bước 1: Lấy tọa độ Tâm (Centroid) của các Quận từ DB
# ═══════════════════════════════════════════════════════════════
def get_district_centroids(conn: psycopg2.extensions.connection) -> list[dict]:
    log.info("[DB] Đang trích xuất tọa độ tâm (Centroid) của tất cả quận/huyện Thái Lan...")
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
                "gid_1": row[0],
                "gid_2": row[1],
                "district_name": row[2],
                "province_name": row[3],
                "lat_center": float(row[4]),
                "lon_center": float(row[5])
            })
            
    log.info(f"[DB] Đã lấy thành công {len(points)} tâm quận/huyện.")
    return points


# ═══════════════════════════════════════════════════════════════
# Bước 2: Gọi API theo batch
# ═══════════════════════════════════════════════════════════════
def fetch_batch(batch_points: list[dict]) -> list[dict]:
    lats = ",".join(str(p["lat_center"]) for p in batch_points)
    lons = ",".join(str(p["lon_center"]) for p in batch_points)

    params = {
        "latitude":   lats,
        "longitude":  lons,
        "start_date": START_DATE,
        "end_date":   END_DATE,
        "daily":      "temperature_2m_mean,relative_humidity_2m_mean",
        "models":     "era5",
        "timezone":   "UTC",
    }

    resp = requests.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict):
        data = [data]

    return data


# ═══════════════════════════════════════════════════════════════
# Bước 3: Parse response → rows (Xử lý Mapping Logic)
# ═══════════════════════════════════════════════════════════════
def parse_response(
    api_response_list: list[dict],
    batch_points: list[dict],
) -> list[tuple]:
    rows: list[tuple] = []
    skipped = 0

    if len(api_response_list) != len(batch_points):
        log.warning(
            f"  [Parse] Mismatch: API trả {len(api_response_list)} objects "
            f"nhưng batch có {len(batch_points)} điểm — fallback bỏ qua batch này"
        )
        return rows

    for point, obj in zip(batch_points, api_response_list):
        daily      = obj.get("daily", {})
        times      = daily.get("time", [])
        # API trả về có đuôi _mean
        temps      = daily.get("temperature_2m_mean", [])
        humidities = daily.get("relative_humidity_2m_mean", [])

        if not times:
            skipped += 1
            continue

        for t, temp, hum in zip(times, temps, humidities):
            observed_at = date.fromisoformat(t)
            rows.append((
                point["gid_1"],       
                point["gid_2"],       
                point["lat_center"],
                point["lon_center"],
                observed_at,
                float(temp) if temp is not None else None,
                float(hum)  if hum  is not None else None,
            ))

    if skipped:
        log.warning(f"  [Parse] {skipped} point(s) không có daily data — bỏ qua")

    return rows


# ═══════════════════════════════════════════════════════════════
# Bước 4: Insert vào PostgreSQL
# ═══════════════════════════════════════════════════════════════
def insert_observations(
    conn: psycopg2.extensions.connection,
    rows: list[tuple],
) -> None:
    if not rows:
        return

    with conn.cursor() as cur:
        # Insert sử dụng tên cột gốc của Database (không có _mean)
        execute_values(
            cur,
            """
            INSERT INTO weather_observations
                (gid_1, gid_2, lat_center, lon_center, observed_at,
                 temperature_2m, relative_humidity_2m)
            VALUES %s
            ON CONFLICT (gid_2, observed_at) DO NOTHING

            """,
            rows,
            page_size=1000,
        )
    conn.commit()


def normalize_observations(conn: psycopg2.extensions.connection) -> None:
    log.info("[DB] Pass 2 — Tính Min-Max normalization ...")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                MIN(temperature_2m),  MAX(temperature_2m),
                MIN(relative_humidity_2m), MAX(relative_humidity_2m)
            FROM weather_observations
        """)
        min_t, max_t, min_h, max_h = cur.fetchone()
        
        if min_t is None: 
            return

        log.info(f"  temp     : [{min_t:.2f}°C → {max_t:.2f}°C]")
        log.info(f"  humidity : [{min_h:.1f}% → {max_h:.1f}%]")

        cur.execute("""
            UPDATE weather_observations
            SET
                temp_nor     = (temperature_2m       - stats.min_t)
                               / NULLIF(stats.max_t       - stats.min_t, 0),
                humidity_nor = (relative_humidity_2m - stats.min_h)
                               / NULLIF(stats.max_h       - stats.min_h, 0)
            FROM (
                SELECT
                    MIN(temperature_2m)       AS min_t,
                    MAX(temperature_2m)       AS max_t,
                    MIN(relative_humidity_2m) AS min_h,
                    MAX(relative_humidity_2m) AS max_h
                FROM weather_observations
            ) AS stats
        """)
        updated = cur.rowcount
    conn.commit()
    log.info(f"[DB] Normalized {updated:,} rows → temp_nor, humidity_nor sẵn sàng")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    log.info("=" * 60)
    log.info("  UNDP Meteorology — Fetch & Insert (Centroid-Based)")
    log.info(f"  Period : {START_DATE} → {END_DATE}")
    log.info(f"  Model  : ERA5  |  Resolution: Daily")
    log.info("=" * 60)

    # ── 1. Kết nối DB ────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công")

    # ── 2. Lấy tọa độ từ DB ──────────────────────────────────
    target_points = get_district_centroids(conn)
    
    if not target_points:
        log.error("[!] Không tìm thấy tọa độ quận nào. Kiểm tra bảng admin_polygons_district.")
        return

    days_expected = (
        (datetime.fromisoformat(END_DATE) - datetime.fromisoformat(START_DATE)).days + 1
    )
    log.info(f"[Info] Kỳ vọng {days_expected} records/điểm × {len(target_points):,} điểm"
             f" = {days_expected * len(target_points):,} rows tổng")

    # ── 3. TRUNCATE Bảng cũ (Để nạp mới hoàn toàn) ────────────
    with conn.cursor() as cur:
        log.info("[DB] Truncating bảng weather_observations ...")
        cur.execute("TRUNCATE TABLE weather_observations RESTART IDENTITY CASCADE")
    conn.commit()

    # ── 4. Fetch + parse + insert theo batch ─────────────────
    n_batches = (len(target_points) + BATCH_SIZE - 1) // BATCH_SIZE
    log.info(f"[Fetch] {n_batches} batch × {BATCH_SIZE} điểm/batch — delay {REQUEST_DELAY}s/batch")
    log.info("-" * 60)

    total_rows      = 0
    failed_batch    = 0
    calls_this_hour = 0
    hour_start      = time.time()

    for i in range(0, len(target_points), BATCH_SIZE):
        batch     = target_points[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        if calls_this_hour + len(batch) > HOURLY_CALL_LIMIT:
            elapsed    = time.time() - hour_start
            sleep_time = max(3600 - elapsed + 15, 0)
            log.info(
                f"[RateLimit] {calls_this_hour} calls trong giờ này "
                f"→ nghỉ {sleep_time:.0f}s đến giờ mới"
            )
            time.sleep(sleep_time)
            calls_this_hour = 0
            hour_start      = time.time()

        log.info(f"  Batch {batch_num:03d}/{n_batches} — {len(batch)} điểm ...")

        try:
            api_data = fetch_batch(batch)
            rows     = parse_response(api_data, batch)
            insert_observations(conn, rows)
            total_rows += len(rows)
            log.info(f"    ✓ {len(rows):,} rows inserted")
            calls_this_hour += len(batch)

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
            time.sleep(REQUEST_DELAY)

    # ── 5. Summary ───────────────────────────────────────────
    log.info("-" * 60)
    log.info(f"[Done] Tổng rows inserted : {total_rows:,}")
    log.info(f"       Batch thất bại     : {failed_batch}/{n_batches}")
    log.info(f"       Kỳ vọng (lý thuyết): {days_expected * len(target_points):,}")

    # ── 6. Chuẩn hóa Data (Normalization) ────────────────────
    normalize_observations(conn)

    # ── 7. Verify DB ─────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM weather_observations")
        obs_count: int = cur.fetchone()[0]

        cur.execute("SELECT MIN(observed_at), MAX(observed_at) FROM weather_observations")
        row = cur.fetchone()
        min_obs, max_obs = row if row else (None, None)

        cur.execute("""
            SELECT
                ROUND(MIN(temp_nor)::numeric,     4), ROUND(MAX(temp_nor)::numeric,     4),
                ROUND(MIN(humidity_nor)::numeric, 4), ROUND(MAX(humidity_nor)::numeric, 4)
            FROM weather_observations
        """)
        res = cur.fetchone()
        nt_min, nt_max, nh_min, nh_max = res if res[0] is not None else ("N/A", "N/A", "N/A", "N/A")

    log.info("")
    log.info("[Verify DB]")
    log.info(f"  weather_observations : {obs_count:,} rows")
    log.info(f"  Date range           : {min_obs} → {max_obs}")
    log.info(f"  temp_nor             : [{nt_min} → {nt_max}]  (kỳ vọng 0.0 → 1.0)")
    log.info(f"  humidity_nor         : [{nh_min} → {nh_max}]  (kỳ vọng 0.0 → 1.0)")

    conn.close()
    log.info("[DB] Connection closed. Done!")


if __name__ == "__main__":
    main()