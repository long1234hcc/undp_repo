"""
fetch_and_insert.py
────────────────────────────────────────────────────────────────
Mục đích:
  1. Tạo grid 0.1°×0.1° bao phủ Bangkok (~30 điểm)
  2. Gọi Open-Meteo Historical API lấy temperature_2m_mean + relative_humidity_2m_mean
     theo độ phân giải NGÀY (daily) thay vì giờ (hourly)
  3. Parse response → từng row riêng biệt (1 row/ngày/điểm)
  4. Insert vào PostgreSQL (bảng grid_points + weather_observations)

Chạy 1 lần duy nhất để seed data.

Yêu cầu:
  pip install requests psycopg2-binary python-dotenv

Cấu hình DB qua biến môi trường (hoặc file .env):
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

Changelog:
  [FIX-1] fetch_batch   : Truyền lat/lon dạng list thay vì join chuỗi
  [FIX-2] fetch_batch   : Đổi era5_land → era5 để cover cả vùng biển ĐNA
  [FIX-3] parse_response: Tìm nearest grid bằng Euclidean distance,
                          không dùng float == (dễ miss khi floating point drift)
  [FIX-4] parse_response: Parse observed_at → datetime thay vì để raw string
  [FIX-5] generate_grid : Tính tâm ô bằng index * STEP thay vì arange + offset
                          để tránh float accumulation drift
  [FIX-6] main          : Thêm continue sau rollback để không dừng toàn bộ job
  [FIX-7] fetch_batch   : timezone UTC thay vì Asia/Bangkok → tránh naive datetime
                          bị PostgreSQL TIMESTAMPTZ hiểu sai múi giờ (+7h offset)
  [FIX-8] fetch_batch   : lat/lon truyền dạng comma-separated string đúng chuẩn
                          Open-Meteo doc, tránh lỗi 414 URI Too Long với list-of-tuples
  [FIX-10] fetch_batch  : Exponential backoff + retry khi gặp 429 Too Many Requests.
                          Đọc Retry-After header nếu có, fallback về jitter backoff.
                          Batch thực sự lỗi (5xx, timeout) mới bị skip.
  [FIX-11] assign_polygon_to_observations:
                          polygon_geom = ST_AsGeoJSON(ap.geom) thay vì ap.geom
                          → lưu GeoJSON string (TEXT) thay vì raw geometry.
  [FIX-12] Đổi bounding box từ Đông Nam Á → Bangkok province
                          LAT: 13.49–14.00, LON: 100.33–100.93 (~30 grid points)
  [FIX-13] fetch_batch  : Đổi từ hourly → daily (temperature_2m_mean,
                          relative_humidity_2m_mean). Theo Open-Meteo docs,
                          &daily= yêu cầu kèm timezone. Dùng timezone=UTC.
  [FIX-14] parse_response: Parse obj["daily"] thay vì obj["hourly"].
                          observed_at là DATE (YYYY-MM-DD) → lưu dạng date object,
                          không replace tzinfo vì daily không có giờ.
  [FIX-15] main         : Đổi biến đếm từ hours_expected → days_expected.
                          1 row/ngày/điểm thay vì 24 rows/ngày/điểm.
  [FIX-16] normalize_observations / insert_observations:
                          Đổi tên column temperature_2m → temperature_2m_mean,
                          relative_humidity_2m → relative_humidity_2m_mean
                          khớp với daily API response và schema DB.
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

# [FIX-12] Bounding box Bangkok province (1,568 km²)
# South: 13.49, North: 14.00, West: 100.33, East: 100.93
LAT_MIN, LAT_MAX = 13.49, 14.00
LON_MIN, LON_MAX = 100.33, 100.93

STEP = 0.01

# Khoảng thời gian lấy data
START_DATE = "2025-01-01"
END_DATE   = "2025-12-31"

# Số điểm mỗi batch — Open-Meteo khuyến nghị ≤ 50
BATCH_SIZE = 50

HOURLY_CALL_LIMIT = 4800
REQUEST_DELAY = 8

API_URL = "https://archive-api.open-meteo.com/v1/archive"


# ═══════════════════════════════════════════════════════════════
# Bước 1: Tạo grid points
# ═══════════════════════════════════════════════════════════════
def generate_grid() -> list[dict]:
    n_lat = int(round((LAT_MAX - LAT_MIN) / STEP))
    n_lon = int(round((LON_MAX - LON_MIN) / STEP))

    points = []
    for i in range(n_lat):
        for j in range(n_lon):
            lat = LAT_MIN + i * STEP + STEP / 2
            lon = LON_MIN + j * STEP + STEP / 2
            lat = round(lat, 6)
            lon = round(lon, 6)

            points.append({
                "lat_center":     lat,
                "lon_center":     lon,
                "lat_edge_south": round(lat - STEP / 2, 6),
                "lat_edge_north": round(lat + STEP / 2, 6),
                "lon_edge_west":  round(lon - STEP / 2, 6),
                "lon_edge_east":  round(lon + STEP / 2, 6),
            })

    log.info(f"[Grid] Tổng {len(points):,} điểm ({n_lat} lat × {n_lon} lon)")
    return points


# ═══════════════════════════════════════════════════════════════
# Bước 2: Gọi API theo batch
# ═══════════════════════════════════════════════════════════════
def fetch_batch(batch_points: list[dict]) -> list[dict]:
    lats = ",".join(str(p["lat_center"]) for p in batch_points)
    lons = ",".join(str(p["lon_center"]) for p in batch_points)

    # [FIX-13] Dùng &daily= thay vì &hourly=
    # Open-Meteo docs: &daily= yêu cầu kèm timezone
    # temperature_2m_mean + relative_humidity_2m_mean là "Additional Daily Variables"
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
# Bước 3: Parse response → rows
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

    for grid_point, obj in zip(batch_points, api_response_list):
        matched_lat = grid_point["lat_center"]
        matched_lon = grid_point["lon_center"]

        # [FIX-14] Parse obj["daily"] thay vì obj["hourly"]
        daily      = obj.get("daily", {})
        times      = daily.get("time", [])
        temps      = daily.get("temperature_2m_mean", [])
        humidities = daily.get("relative_humidity_2m_mean", [])

        if not times:
            skipped += 1
            continue

        for t, temp, hum in zip(times, temps, humidities):
            # [FIX-14] Daily trả "YYYY-MM-DD" (không có giờ) → parse thành date object
            # Lưu dạng date để PostgreSQL nhận vào column DATE hoặc TIMESTAMPTZ
            observed_at = date.fromisoformat(t)
            rows.append((
                matched_lat,
                matched_lon,
                observed_at,
                float(temp) if temp is not None else None,
                float(hum)  if hum  is not None else None,
            ))

    if skipped:
        log.warning(f"  [Parse] {skipped} point(s) không có daily data — bỏ qua")

    return rows


def assign_polygon_to_observations(conn: psycopg2.extensions.connection) -> None:
    """
    Pass 3: Gán gid_1 + polygon_geom vào weather_observations.

    Dùng PostGIS ST_Contains để map từng grid point → tỉnh tương ứng.
    [FIX-11] ST_AsGeoJSON(ap.geom) thay vì ap.geom → lưu GeoJSON string (TEXT).

    Các điểm ngoài ranh giới admin → gid_1 = NULL, polygon_geom = NULL.
    """
    log.info("[DB] Pass 3 — Gán gid_1 + polygon_geom ...")

    with conn.cursor() as cur:
        # ── Step 3a: Gán gid_1 cho grid_points trước ─────────
        cur.execute("""
            UPDATE grid_points gp
            SET gid_1 = ap.gid_1
            FROM admin_polygons ap
            WHERE ST_Contains(
                ap.geom,
                ST_SetSRID(ST_Point(gp.lon_center, gp.lat_center), 4326)
            )
        """)
        gp_updated = cur.rowcount
        log.info(f"  [3a] {gp_updated:,} grid_points được gán gid_1")

    conn.commit()


    # ── Step 3c: Gán gid_2 + geom_district ───────────────────
    # Dùng ST_Within: grid point nằm trong district nào → gán gid_2
    # Fallback nearest neighbor cho các điểm nằm trên boundary
    with conn.cursor() as cur:
        # 3c-1: ST_Within (chính xác)
        cur.execute("""
            UPDATE weather_observations wo
            SET
                gid_2        = apd.gid_2,
                geom_district = ST_AsText(apd.geom)
            FROM admin_polygons_district apd
            WHERE ST_Within(
                ST_SetSRID(ST_Point(wo.lon_center, wo.lat_center), 4326),
                apd.geom
            )
        """)
        within_updated = cur.rowcount
        log.info(f"  [3c-1] {within_updated:,} observations gán gid_2 bằng ST_Within")
    conn.commit()

    with conn.cursor() as cur:
        # ── Step 3b: Propagate gid_1 + polygon_geom → weather_observations ──
        # [FIX-11] ST_AsGeoJSON → TEXT thay vì raw geometry
        # [FIX-12] ST_NumGeometries = 1 → extract Polygon đơn thay vì MultiPolygon
        cur.execute("""
            UPDATE weather_observations wo
            SET
                gid_1        = ap.gid_1,
                polygon_geom = ST_AsGeoJSON(
                    CASE
                        WHEN ST_NumGeometries(ap.geom) = 1 THEN ST_GeometryN(ap.geom, 1)
                        ELSE ap.geom
                    END
                )
            FROM grid_points gp
            JOIN admin_polygons ap ON gp.gid_1 = ap.gid_1
            WHERE wo.lat_center = gp.lat_center
              AND wo.lon_center = gp.lon_center
        """)
        wo_updated = cur.rowcount
        log.info(f"  [3b] {wo_updated:,} weather_observations được gán gid_1 + polygon_geom")

    conn.commit()

    # ── Verify ────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM weather_observations WHERE gid_1 IS NOT NULL
        """)
        with_polygon = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*) FROM weather_observations WHERE gid_1 IS NULL
        """)
        no_polygon = cur.fetchone()[0]

        cur.execute("""
            SELECT ap.country_code, COUNT(DISTINCT gp.gid_1) as provinces
            FROM grid_points gp
            JOIN admin_polygons ap ON gp.gid_1 = ap.gid_1
            GROUP BY ap.country_code
            ORDER BY ap.country_code
        """)
        by_country = cur.fetchall()

    log.info(f"  [Verify] {with_polygon:,} rows có polygon  |  {no_polygon:,} rows không có polygon (NULL)")
    log.info("  [Verify] Phân bổ theo quốc gia:")
    for code, n in by_country:
        log.info(f"    {code:<6} {n:>3} provinces có data")

    log.info("[DB] Pass 3 hoàn tất")


# ═══════════════════════════════════════════════════════════════
# Bước 4: Insert vào PostgreSQL
# ═══════════════════════════════════════════════════════════════
def insert_grid_points(conn: psycopg2.extensions.connection, grid_points: list[dict]) -> None:
    rows = [
        (
            p["lat_center"], p["lon_center"],
            p["lat_edge_south"], p["lat_edge_north"],
            p["lon_edge_west"],  p["lon_edge_east"],
        )
        for p in grid_points
    ]

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO grid_points
                (lat_center, lon_center,
                 lat_edge_south, lat_edge_north,
                 lon_edge_west,  lon_edge_east)
            VALUES %s
            ON CONFLICT (lat_center, lon_center) DO NOTHING
            """,
            rows,
            page_size=500,
        )
    conn.commit()
    log.info(f"[DB] Upserted {len(rows):,} grid points")


def insert_observations(
    conn: psycopg2.extensions.connection,
    rows: list[tuple],
) -> None:
    if not rows:
        return

    with conn.cursor() as cur:
        # [FIX-16] Column names đổi sang _mean để khớp daily API
        execute_values(
            cur,
            """
            INSERT INTO weather_observations
                (lat_center, lon_center, observed_at,
                 temperature_2m, relative_humidity_2m)
            VALUES %s
            ON CONFLICT (lat_center, lon_center, observed_at) DO NOTHING
            """,
            rows,
            page_size=1000,
        )
    conn.commit()


def normalize_observations(conn: psycopg2.extensions.connection) -> None:
    """
    Pass 2: Điền temp_nor và humidity_nor bằng Min-Max Normalization.
    """
    log.info("[DB] Pass 2 — Tính Min-Max normalization ...")

    with conn.cursor() as cur:
        # [FIX-16] Dùng tên column _mean
        cur.execute("""
            SELECT
                MIN(temperature_2m),  MAX(temperature_2m),
                MIN(relative_humidity_2m), MAX(relative_humidity_2m)
            FROM weather_observations
        """)
        min_t, max_t, min_h, max_h = cur.fetchone()
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
    log.info("  UNDP Meteorology — Fetch & Insert")
    log.info(f"  Period : {START_DATE} → {END_DATE}")
    log.info(f"  Grid   : {STEP}° × {STEP}°  |  Model: ERA5  |  Resolution: Daily")
    log.info(f"  Region : Bangkok  ({LAT_MIN}–{LAT_MAX}°N, {LON_MIN}–{LON_MAX}°E)")
    log.info("=" * 60)

    # ── 1. Tạo grid ─────────────────────────────────────────
    grid_points = generate_grid()

    # [FIX-15] Daily: 1 record/ngày/điểm (không phải 24)
    days_expected = (
        (datetime.fromisoformat(END_DATE) - datetime.fromisoformat(START_DATE)).days + 1
    )
    log.info(f"[Info] Kỳ vọng {days_expected} records/điểm × {len(grid_points):,} điểm"
             f" = {days_expected * len(grid_points):,} rows tổng")

    # ── 2. Kết nối DB ────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công")

    # ── 3. TRUNCATE + Insert grid points ────────────────────────
    with conn.cursor() as cur:
        log.info("[DB] Truncating tables ...")
        cur.execute("TRUNCATE TABLE weather_observations, grid_points RESTART IDENTITY CASCADE")
    conn.commit()
    insert_grid_points(conn, grid_points)

    # ── 4. Fetch + parse + insert theo batch ─────────────────
    n_batches = (len(grid_points) + BATCH_SIZE - 1) // BATCH_SIZE
    log.info(f"[Fetch] {n_batches} batch × {BATCH_SIZE} điểm/batch — delay {REQUEST_DELAY}s/batch")
    log.info("-" * 60)

    total_rows      = 0
    failed_batch    = 0
    calls_this_hour = 0
    hour_start      = time.time()

    for i in range(0, len(grid_points), BATCH_SIZE):
        batch     = grid_points[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        # ── Hourly rate limit guard ──────────────────────────
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
    log.info(f"       Kỳ vọng (lý thuyết): {days_expected * len(grid_points):,}")

    # ── 6. Pass 2: Normalization ─────────────────────────────
    normalize_observations(conn)

    # ── 7. Pass 3: Assign polygon ────────────────────────────
    assign_polygon_to_observations(conn)

    # ── 8. Verify DB ─────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM grid_points")
        gp_count: int = cur.fetchone()[0]

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
        nt_min, nt_max, nh_min, nh_max = cur.fetchone()

    log.info("")
    log.info("[Verify DB]")
    log.info(f"  grid_points          : {gp_count:,} rows")
    log.info(f"  weather_observations : {obs_count:,} rows")
    log.info(f"  Date range           : {min_obs} → {max_obs}")
    log.info(f"  temp_nor             : [{nt_min} → {nt_max}]  (kỳ vọng 0.0 → 1.0)")
    log.info(f"  humidity_nor         : [{nh_min} → {nh_max}]  (kỳ vọng 0.0 → 1.0)")

    conn.close()
    log.info("[DB] Connection closed. Done!")


if __name__ == "__main__":
    main()