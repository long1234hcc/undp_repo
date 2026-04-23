"""
fetch_and_insert.py
────────────────────────────────────────────────────────────────
Mục đích:
  1. Tạo grid 1°x1° bao phủ Đông Nam Á (1,748 điểm)
  2. Gọi Open-Meteo Historical API lấy temperature_2m + relative_humidity_2m
  3. Parse response → từng row riêng biệt
  4. Insert vào PostgreSQL (bảng grid_points + weather_observations)

Chạy 1 lần duy nhất để seed data.

Yêu cầu:
  pip install requests psycopg2-binary python-dotenv

Cấu hình DB qua biến môi trường (hoặc file .env):
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

────────────────────────────────────────────────────────────────
"""

import os
import time
import logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone

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

# Bounding box Đông Nam Á
LAT_MIN, LAT_MAX = -10.0, 28.0
LON_MIN, LON_MAX =  95.0, 141.0
STEP = 0.2

# Khoảng thời gian lấy data
START_DATE = "2026-04-01"
END_DATE   = "2026-04-07"

# Số điểm mỗi batch — Open-Meteo khuyến nghị ≤ 50
BATCH_SIZE = 50

# Delay giữa các batch (giây) — tránh rate limit
REQUEST_DELAY = 6.5

API_URL = "https://archive-api.open-meteo.com/v1/archive"


# ═══════════════════════════════════════════════════════════════
# Bước 1: Tạo grid points
# ═══════════════════════════════════════════════════════════════
def generate_grid() -> list[dict]:
    """
    Tạo danh sách các ô grid 1°x1° bao phủ bounding box ĐNA.

    Dùng index * STEP thay vì np.arange() + offset để tránh
    floating-point accumulation drift qua nhiều phép cộng liên tiếp.
    Kết quả: tâm ô luôn là X.5 (e.g. -9.5, -8.5, ..., 27.5).

    FIX-5: Tính lat/lon từ index thay vì arange để tránh float drift.
    """
    n_lat = int(round((LAT_MAX - LAT_MIN) / STEP))
    n_lon = int(round((LON_MAX - LON_MIN) / STEP))

    points = []
    for i in range(n_lat):
        for j in range(n_lon):
            # Tâm ô = cạnh dưới/trái + nửa bước
            lat = LAT_MIN + i * STEP + STEP / 2
            lon = LON_MIN + j * STEP + STEP / 2

            # Round 6 chữ số: đủ chính xác, loại bỏ noise float 64-bit
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
    """
    Gọi Open-Meteo Archive API cho 1 batch (tối đa BATCH_SIZE điểm).
    Trả về list of response objects theo đúng thứ tự batch_points.
    """
    lats = ",".join(str(p["lat_center"]) for p in batch_points)  # FIX-8
    lons = ",".join(str(p["lon_center"]) for p in batch_points)  # FIX-8

    params = {
        "latitude":   lats,
        "longitude":  lons,
        "start_date": START_DATE,
        "end_date":   END_DATE,
        "hourly":     "temperature_2m,relative_humidity_2m",
        "models":     "era5",       
        "timezone":   "UTC",       
    }

    resp = requests.get(API_URL, params=params, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    # Nếu chỉ 1 điểm, API trả về dict; nhiều điểm trả về list
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
    """
    Chuyển list response objects từ API thành list rows để INSERT.

    Mỗi row: (lat_center, lon_center, observed_at, temperature_2m, relative_humidity_2m)
    """
    rows: list[tuple] = []
    skipped = 0

    # Kiểm tra độ dài khớp — bảo vệ nếu API thay đổi hành vi
    if len(api_response_list) != len(batch_points):
        log.warning(
            f"  [Parse] Mismatch: API trả {len(api_response_list)} objects "
            f"nhưng batch có {len(batch_points)} điểm — fallback bỏ qua batch này"
        )
        return rows

    for grid_point, obj in zip(batch_points, api_response_list):  # FIX-9
        matched_lat = grid_point["lat_center"]
        matched_lon = grid_point["lon_center"]

        hourly     = obj.get("hourly", {})
        times      = hourly.get("time", [])
        temps      = hourly.get("temperature_2m", [])
        humidities = hourly.get("relative_humidity_2m", [])

        if not times:
            skipped += 1
            continue

        for t, temp, hum in zip(times, temps, humidities):
            # FIX-4 + FIX-7: parse naive string UTC → aware datetime UTC
            observed_at = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            rows.append((
                matched_lat,
                matched_lon,
                observed_at,
                float(temp) if temp is not None else None,
                float(hum)  if hum  is not None else None,
            ))

    if skipped:
        log.warning(f"  [Parse] {skipped} point(s) không có hourly data — bỏ qua")

    return rows


# ═══════════════════════════════════════════════════════════════
# Bước 4: Insert vào PostgreSQL
# ═══════════════════════════════════════════════════════════════
def insert_grid_points(conn: psycopg2.extensions.connection, grid_points: list[dict]) -> None:
    """
    Upsert tất cả grid points vào bảng grid_points.
    ON CONFLICT DO NOTHING → an toàn khi chạy lại nhiều lần.
    """
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
    """
    Bulk-insert weather observations.
    ON CONFLICT DO NOTHING → idempotent nếu chạy lại.
    """
    if not rows:
        return

    with conn.cursor() as cur:
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


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    log.info("=" * 60)
    log.info("  UNDP Meteorology — Fetch & Insert")
    log.info(f"  Period : {START_DATE} → {END_DATE}")
    log.info(f"  Grid   : {STEP}° × {STEP}°  |  Model: ERA5")
    log.info("=" * 60)

    # ── 1. Tạo grid ─────────────────────────────────────────
    grid_points = generate_grid()

    # Số giờ kỳ vọng = 7 ngày × 24h = 168
    hours_expected = (
        (datetime.fromisoformat(END_DATE) - datetime.fromisoformat(START_DATE)).days + 1
    ) * 24
    log.info(f"[Info] Kỳ vọng {hours_expected} records/điểm × {len(grid_points):,} điểm"
             f" = {hours_expected * len(grid_points):,} rows tổng")

    # ── 2. Kết nối DB ────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công")

    # ── 3. Insert grid points ────────────────────────────────
    insert_grid_points(conn, grid_points)

    # ── 4. Fetch + parse + insert theo batch ─────────────────
    n_batches = (len(grid_points) + BATCH_SIZE - 1) // BATCH_SIZE
    log.info(f"[Fetch] {n_batches} batch × {BATCH_SIZE} điểm/batch")
    log.info("-" * 60)

    total_rows   = 0
    failed_batch = 0

    for i in range(0, len(grid_points), BATCH_SIZE):
        batch     = grid_points[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        log.info(f"  Batch {batch_num:03d}/{n_batches} — {len(batch)} điểm ...")

        try:
            api_data = fetch_batch(batch)
            rows     = parse_response(api_data, batch)  # FIX-9: pass batch trực tiếp
            insert_observations(conn, rows)
            total_rows += len(rows)
            log.info(f"    ✓ {len(rows):,} rows inserted")

        except requests.HTTPError as e:
            log.error(f"    ✗ HTTP Error: {e.response.status_code} — {e}")
            failed_batch += 1
            # Không cần rollback vì chưa INSERT gì ở batch này
            # FIX-6: continue để không dừng toàn bộ job
            continue

        except requests.RequestException as e:
            log.error(f"    ✗ Request Error: {e}")
            failed_batch += 1
            continue

        except Exception as e:
            log.error(f"    ✗ Unexpected Error: {e}", exc_info=True)
            failed_batch += 1
            conn.rollback()   # rollback transaction dở dang nếu có
            continue          # FIX-6: tiếp tục batch tiếp theo

        finally:
            # Delay giữa các batch dù thành công hay thất bại
            time.sleep(REQUEST_DELAY)

    # ── 5. Summary ───────────────────────────────────────────
    log.info("-" * 60)
    log.info(f"[Done] Tổng rows inserted : {total_rows:,}")
    log.info(f"       Batch thất bại     : {failed_batch}/{n_batches}")
    log.info(f"       Kỳ vọng (lý thuyết): {hours_expected * len(grid_points):,}")

    # ── 6. Verify DB ─────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM grid_points")
        gp_count: int = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM weather_observations")
        obs_count: int = cur.fetchone()[0]

        cur.execute("SELECT MIN(observed_at), MAX(observed_at) FROM weather_observations")
        row = cur.fetchone()
        min_t, max_t = row if row else (None, None)

    log.info("")
    log.info("[Verify DB]")
    log.info(f"  grid_points          : {gp_count:,} rows")
    log.info(f"  weather_observations : {obs_count:,} rows")
    log.info(f"  Time range           : {min_t} → {max_t}")

    conn.close()
    log.info("[DB] Connection closed. Done!")


if __name__ == "__main__":
    main()
    # print(generate_grid())