"""
UNDP Meteorology — Unified Weather Pipeline
════════════════════════════════════════════════════════════════════════
Pipeline duy nhất thực hiện tuần tự 2 stage:

  STAGE 1 — Forecast Fetch
    • Lấy centroid các quận Bangkok từ bảng admin_polygons_district
    • Gọi Open-Meteo Forecast API (daily, 4 ngày: hôm nay + 3 ngày tới)
    • Tính mean = (max + min) / 2 cho temperature & relative_humidity
    • TRUNCATE forecast_run_date = today → INSERT weather_forecasts

  STAGE 2 — Heat Index Alert
    • Tính Heat Index = f(temperature_2m, relative_humidity_2m)
      theo công thức Rothfusz / NOAA — chuẩn quốc tế,
      được Thai Meteorological Department + BMA Bangkok áp dụng.
    • Gán alert_level theo 5 mức:
        NORMAL         HI < 27°C    — Bình thường
        CAUTION        27–33°C      — Mệt mỏi khi hoạt động kéo dài
        WARNING        33–42°C      — Chuột rút, kiệt sức do nhiệt
        DANGER         42–52°C      — Nguy cơ say nắng cao
        EXTREME_DANGER HI ≥ 52°C   — Cực kỳ nguy hiểm, dừng hoạt động ngoài trời
    • TRUNCATE forecast_run_date = today → INSERT weather_alerts

════════════════════════════════════════════════════════════════════════
Schema: weather_forecasts
  id                    SERIAL PRIMARY KEY
  gid_1                 TEXT
  gid_2                 TEXT
  lat_center            NUMERIC
  lon_center            NUMERIC
  forecast_run_date     DATE        -- Ngày script chạy
  forecast_date         DATE        -- Ngày được dự báo
  temperature_2m        NUMERIC     -- Mean = (max + min) / 2  (°C)
  relative_humidity_2m  NUMERIC     -- Mean = (max + min) / 2  (%)
  UNIQUE (gid_2, forecast_date, forecast_run_date)

Schema: weather_alerts
  id                    SERIAL PRIMARY KEY
  gid_1                 TEXT
  gid_2                 TEXT
  district_name         TEXT
  forecast_run_date     DATE
  forecast_date         DATE
  temperature_2m        NUMERIC     -- Nhiệt độ thực (°C)
  relative_humidity_2m  NUMERIC     -- Độ ẩm thực (%)
  heat_index            NUMERIC     -- Feels-like temperature (°C)
  alert_level           TEXT        -- NORMAL/CAUTION/WARNING/DANGER/EXTREME_DANGER
  created_at            TIMESTAMPTZ DEFAULT now()
  UNIQUE (gid_2, forecast_date, forecast_run_date)
════════════════════════════════════════════════════════════════════════
"""

import os
import time
import logging
import requests
import psycopg2
from psycopg2.extras import execute_values
from datetime import date, timedelta

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

# ── Forecast Config ──────────────────────────────────────────────────
FORECAST_DAYS = 4    # Hôm nay + 3 ngày tới
BATCH_SIZE    = 30   # Số quận mỗi batch API call
REQUEST_DELAY = 40   # Giây nghỉ giữa các batch (tránh rate limit)

API_URL = "https://api.open-meteo.com/v1/forecast"

# ── Heat Index Thresholds ────────────────────────────────────────────
# Nguồn: NOAA Heat Index scale + Thai Meteorological Dept / BMA Bangkok
# Mỗi tuple: (lower_bound_inclusive, alert_level)
# Duyệt từ trên xuống — match đầu tiên thắng.
HI_THRESHOLDS = [
    (52.0, "EXTREME_DANGER"),  # HI ≥ 52°C : Cực kỳ nguy hiểm
    (42.0, "DANGER"),          # 42 ≤ HI < 52°C : Nguy cơ say nắng cao
    (33.0, "WARNING"),         # 33 ≤ HI < 42°C : Chuột rút, kiệt sức
    (27.0, "CAUTION"),         # 27 ≤ HI < 33°C : Mệt mỏi khi hoạt động kéo dài
    (0.0,  "NORMAL"),          # HI < 27°C      : Bình thường
]


# ════════════════════════════════════════════════════════════════════
# UTILITIES
# ════════════════════════════════════════════════════════════════════

def compute_heat_index(t: float, rh: float) -> float:
    """
    Tính Heat Index (°C) theo công thức Rothfusz — chuẩn NOAA.

    Heat Index phản ánh "feels-like temperature" khi kết hợp
    cả nhiệt độ lẫn độ ẩm. Ví dụ: 36°C + 70% RH → HI ≈ 50°C.

    Điều kiện áp dụng công thức Rothfusz:
      - T ≥ 27°C  (80°F) — dưới mức này HI ≈ T
      - RH ≥ 40%         — không khí đủ ẩm để có hiệu ứng

    Tham số:
        t  : nhiệt độ thực (°C)
        rh : relative humidity (%)
    Trả về:
        heat_index (°C), làm tròn 2 chữ số thập phân
    """
    if t < 27.0 or rh < 40.0:
        # Điều kiện mát hoặc khô: HI ≈ nhiệt độ thực
        return round(t, 2)

    # Chuyển sang °F để dùng hệ số Rothfusz (định nghĩa gốc theo °F)
    tf = t * 9.0 / 5.0 + 32.0
    r  = rh

    hi_f = (
        -42.379
        + 2.04901523   * tf
        + 10.14333127  * r
        - 0.22475541   * tf * r
        - 0.00683783   * tf ** 2
        - 0.05481717   * r  ** 2
        + 0.00122874   * tf ** 2 * r
        + 0.00085282   * tf * r  ** 2
        - 0.00000199   * tf ** 2 * r ** 2
    )

    # Điều chỉnh bổ sung theo NOAA
    if rh < 13.0 and 80.0 <= tf <= 112.0:
        # Không khí rất khô: HI thực tế thấp hơn
        adjustment = ((13.0 - rh) / 4.0) * ((17.0 - abs(tf - 95.0)) / 17.0) ** 0.5
        hi_f -= adjustment
    elif rh > 85.0 and 80.0 <= tf <= 87.0:
        # Nhiệt độ thấp + độ ẩm rất cao: HI thực tế cao hơn
        adjustment = ((rh - 85.0) / 10.0) * ((87.0 - tf) / 5.0)
        hi_f += adjustment

    # Chuyển lại °C
    hi_c = (hi_f - 32.0) * 5.0 / 9.0
    return round(hi_c, 2)


def get_alert_level(heat_index: float) -> str:
    """Tra HI_THRESHOLDS từ cao xuống thấp, trả về alert_level đầu tiên khớp."""
    for threshold, level in HI_THRESHOLDS:
        if heat_index >= threshold:
            return level
    return "NORMAL"


# ════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ════════════════════════════════════════════════════════════════════

def ensure_tables(conn: psycopg2.extensions.connection) -> None:
    """Tạo cả 2 bảng nếu chưa tồn tại."""
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_alerts (
                id                    SERIAL PRIMARY KEY,
                gid_1                 TEXT,
                gid_2                 TEXT,
                district_name         TEXT,
                forecast_run_date     DATE,
                forecast_date         DATE,
                temperature_2m        NUMERIC,
                relative_humidity_2m  NUMERIC,
                heat_index            NUMERIC,
                alert_level           TEXT,
                created_at            TIMESTAMPTZ DEFAULT now(),
                UNIQUE (gid_2, forecast_date, forecast_run_date)
            );
        """)
    conn.commit()
    log.info("[DB] Bảng weather_forecasts & weather_alerts đã sẵn sàng.")


def truncate_today(
    conn:     psycopg2.extensions.connection,
    table:    str,
    run_date: date,
) -> None:
    """Xóa toàn bộ record forecast_run_date = run_date trong bảng chỉ định."""
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM {table} WHERE forecast_run_date = %s",
            (run_date,),
        )
        deleted = cur.rowcount
    conn.commit()
    log.info(f"[DB] Truncate {table} (run_date={run_date}): xóa {deleted:,} rows.")


# ════════════════════════════════════════════════════════════════════
# STAGE 1 — FORECAST FETCH
# ════════════════════════════════════════════════════════════════════

def get_district_centroids(conn: psycopg2.extensions.connection) -> list[dict]:
    """Lấy tọa độ tâm (centroid) các quận Bangkok từ DB."""
    log.info("[Stage 1] Trích xuất centroid các quận Bangkok ...")
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
            WHERE country_code  = 'THA'
              AND province_name = 'BangkokMetropolis'
        """)
        rows = cur.fetchall()

    points = [
        {
            "gid_1":         r[0],
            "gid_2":         r[1],
            "district_name": r[2],
            "province_name": r[3],
            "lat_center":    float(r[4]),
            "lon_center":    float(r[5]),
        }
        for r in rows
    ]
    log.info(f"[Stage 1] {len(points)} quận tìm thấy.")
    return points


def fetch_batch(batch_points: list[dict]) -> list[dict]:
    """Gọi Open-Meteo API cho 1 batch điểm, trả về list response object."""
    params = {
        "latitude":  ",".join(str(p["lat_center"]) for p in batch_points),
        "longitude": ",".join(str(p["lon_center"]) for p in batch_points),
        "daily": ",".join([
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
    # API trả dict nếu 1 điểm, list nếu nhiều điểm
    return [data] if isinstance(data, dict) else data


def parse_forecast_response(
    api_data:     list[dict],
    batch_points: list[dict],
    run_date:     date,
) -> list[tuple]:
    """Parse API response → list tuples sẵn sàng INSERT vào weather_forecasts."""
    if len(api_data) != len(batch_points):
        log.warning(
            f"[Stage 1] Mismatch: API={len(api_data)} objects "
            f"vs batch={len(batch_points)} điểm — bỏ qua batch này."
        )
        return []

    rows: list[tuple] = []
    for point, obj in zip(batch_points, api_data):
        daily  = obj.get("daily", {})
        times  = daily.get("time", [])
        t_maxs = daily.get("temperature_2m_max", [])
        t_mins = daily.get("temperature_2m_min", [])
        h_maxs = daily.get("relative_humidity_2m_max", [])
        h_mins = daily.get("relative_humidity_2m_min", [])

        if not times:
            log.warning(f"[Stage 1] Không có daily data cho gid_2={point['gid_2']} — bỏ qua.")
            continue

        for t_str, t_max, t_min, h_max, h_min in zip(times, t_maxs, t_mins, h_maxs, h_mins):
            temp_mean = (
                round((float(t_max) + float(t_min)) / 2, 4)
                if t_max is not None and t_min is not None else None
            )
            hum_mean = (
                round((float(h_max) + float(h_min)) / 2, 4)
                if h_max is not None and h_min is not None else None
            )
            rows.append((
                point["gid_1"],
                point["gid_2"],
                point["lat_center"],
                point["lon_center"],
                run_date,                    # forecast_run_date
                date.fromisoformat(t_str),   # forecast_date
                temp_mean,                   # temperature_2m
                hum_mean,                    # relative_humidity_2m
            ))
    return rows


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


def run_stage1(
    conn:     psycopg2.extensions.connection,
    run_date: date,
) -> int:
    """
    Stage 1: Fetch forecast → truncate today → insert.
    Trả về tổng số rows đã inserted.
    """
    log.info("")
    log.info("━" * 60)
    log.info("  STAGE 1 — Forecast Fetch & Insert")
    log.info("━" * 60)

    target_points = get_district_centroids(conn)
    if not target_points:
        log.error("[Stage 1] Không tìm thấy quận nào. Kiểm tra bảng admin_polygons_district.")
        return 0

    n_batches  = (len(target_points) + BATCH_SIZE - 1) // BATCH_SIZE
    total_rows = 0
    n_failed   = 0

    log.info(
        f"[Stage 1] {len(target_points)} quận × {FORECAST_DAYS} ngày "
        f"= {len(target_points) * FORECAST_DAYS:,} rows kỳ vọng"
    )
    log.info(f"[Stage 1] {n_batches} batch × {BATCH_SIZE} quận — delay {REQUEST_DELAY}s/batch")

    # Truncate dữ liệu hôm nay trước khi fetch
    truncate_today(conn, "weather_forecasts", run_date)

    for i in range(0, len(target_points), BATCH_SIZE):
        batch     = target_points[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        log.info(f"  Batch {batch_num:03d}/{n_batches} — {len(batch)} điểm ...")
        try:
            api_data = fetch_batch(batch)
            rows     = parse_forecast_response(api_data, batch, run_date)
            insert_forecasts(conn, rows)
            total_rows += len(rows)
            log.info(f"    ✓ {len(rows):,} rows inserted")

        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "N/A"
            log.error(f"    ✗ HTTP {status} — bỏ qua batch {batch_num}")
            n_failed += 1

        except requests.RequestException as e:
            log.error(f"    ✗ Request Error: {e} — bỏ qua batch {batch_num}")
            n_failed += 1

        except Exception as e:
            log.error(f"    ✗ Unexpected Error: {e}", exc_info=True)
            n_failed += 1
            conn.rollback()

        finally:
            # Không sleep sau batch cuối
            if i + BATCH_SIZE < len(target_points):
                time.sleep(REQUEST_DELAY)

    log.info(
        f"[Stage 1] ✅ Hoàn tất — {total_rows:,} rows inserted | "
        f"{n_failed}/{n_batches} batch thất bại"
    )
    return total_rows


# ════════════════════════════════════════════════════════════════════
# STAGE 2 — HEAT INDEX ALERT
# ════════════════════════════════════════════════════════════════════

def fetch_forecasts_for_alert(
    conn:     psycopg2.extensions.connection,
    run_date: date,
) -> list[dict]:
    """
    Đọc toàn bộ weather_forecasts của run_date hôm nay.
    Join admin_polygons_district để lấy district_name.
    Không filter theo ngưỡng — tính HI và gán level cho TẤT CẢ records.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                wf.gid_1,
                wf.gid_2,
                apd.district_name,
                wf.forecast_run_date,
                wf.forecast_date,
                wf.temperature_2m,
                wf.relative_humidity_2m
            FROM weather_forecasts wf
            LEFT JOIN admin_polygons_district apd
                ON wf.gid_2 = apd.gid_2
            WHERE wf.forecast_run_date  = %s
              AND wf.temperature_2m       IS NOT NULL
              AND wf.relative_humidity_2m IS NOT NULL
            ORDER BY wf.forecast_date, wf.gid_2
        """, (run_date,))
        rows = cur.fetchall()

    return [
        {
            "gid_1":               r[0],
            "gid_2":               r[1],
            "district_name":       r[2],
            "forecast_run_date":   r[3],
            "forecast_date":       r[4],
            "temperature_2m":      float(r[5]),
            "relative_humidity_2m": float(r[6]),
        }
        for r in rows
    ]


def build_alert_rows(forecasts: list[dict]) -> list[tuple]:
    """Tính Heat Index + Alert Level cho từng record, trả về list tuples."""
    rows = []
    for f in forecasts:
        hi    = compute_heat_index(f["temperature_2m"], f["relative_humidity_2m"])
        level = get_alert_level(hi)
        rows.append((
            f["gid_1"],
            f["gid_2"],
            f["district_name"],
            f["forecast_run_date"],
            f["forecast_date"],
            f["temperature_2m"],
            f["relative_humidity_2m"],
            hi,
            level,
        ))
    return rows


def insert_alerts(
    conn: psycopg2.extensions.connection,
    rows: list[tuple],
) -> None:
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO weather_alerts
                (gid_1, gid_2, district_name,
                 forecast_run_date, forecast_date,
                 temperature_2m, relative_humidity_2m,
                 heat_index, alert_level)
            VALUES %s
            ON CONFLICT (gid_2, forecast_date, forecast_run_date)
            DO UPDATE SET
                temperature_2m       = EXCLUDED.temperature_2m,
                relative_humidity_2m = EXCLUDED.relative_humidity_2m,
                heat_index           = EXCLUDED.heat_index,
                alert_level          = EXCLUDED.alert_level,
                created_at           = now()
            """,
            rows,
            page_size=1000,
        )
    conn.commit()


def print_alert_summary(
    conn:     psycopg2.extensions.connection,
    run_date: date,
) -> None:
    """In summary alert ra log sau khi insert xong."""
    with conn.cursor() as cur:

        # Breakdown theo alert_level
        cur.execute("""
            SELECT
                alert_level,
                COUNT(*)                                        AS n_records,
                ROUND(AVG(heat_index)::numeric,          2)    AS avg_hi,
                ROUND(MAX(heat_index)::numeric,          2)    AS max_hi,
                ROUND(AVG(temperature_2m)::numeric,      2)    AS avg_temp,
                ROUND(AVG(relative_humidity_2m)::numeric,2)    AS avg_rh
            FROM weather_alerts
            WHERE forecast_run_date = %s
            GROUP BY alert_level
            ORDER BY max_hi DESC
        """, (run_date,))
        level_stats = cur.fetchall()

        # Breakdown theo ngày forecast
        cur.execute("""
            SELECT
                forecast_date,
                COUNT(DISTINCT gid_2)              AS n_districts,
                ROUND(MAX(heat_index)::numeric, 2) AS max_hi,
                MAX(alert_level)                   AS worst_level
            FROM weather_alerts
            WHERE forecast_run_date = %s
            GROUP BY forecast_date
            ORDER BY forecast_date
        """, (run_date,))
        by_day = cur.fetchall()

        # Top 5 quận có Heat Index cao nhất hôm nay
        cur.execute("""
            SELECT
                district_name,
                temperature_2m,
                relative_humidity_2m,
                heat_index,
                alert_level
            FROM weather_alerts
            WHERE forecast_run_date = %s
              AND forecast_date     = %s
            ORDER BY heat_index DESC
            LIMIT 5
        """, (run_date, run_date))
        top_today = cur.fetchall()

    W = 66  # độ rộng box log

    def box(text: str) -> str:
        return f"║  {text:<{W - 4}}║"

    log.info("")
    log.info("╔" + "═" * (W - 2) + "╗")
    log.info(box("WEATHER ALERT SUMMARY  (Heat Index Based)"))
    log.info(box(f"Run date  : {run_date}"))
    log.info(box("Formula   : NOAA Rothfusz  HI = f(temperature, humidity)"))
    log.info("╠" + "═" * (W - 2) + "╣")

    # Alert Level Breakdown
    log.info(box("Alert Level Breakdown:"))
    log.info(box(f"  {'Level':<16} {'Records':>7}  {'Avg HI':>8}  {'Max HI':>8}  {'Avg Temp':>9}  {'Avg RH':>7}"))
    log.info(box("  " + "─" * 58))
    for level, n, avg_hi, max_hi, avg_t, avg_rh in level_stats:
        log.info(box(
            f"  {level:<16} {n:>7,}  {avg_hi:>7.1f}°C  {max_hi:>7.1f}°C"
            f"  {avg_t:>8.1f}°C  {avg_rh:>6.1f}%"
        ))
    if not level_stats:
        log.info(box("  (Không có dữ liệu)"))

    # Breakdown theo ngày
    log.info("╠" + "═" * (W - 2) + "╣")
    log.info(box("Theo ngày forecast:"))
    log.info(box(f"  {'Date':<12} {'Districts':>10}  {'Max HI':>8}  {'Worst Level':<16}"))
    log.info(box("  " + "─" * 52))
    for fc_date, n_dist, max_hi, worst in by_day:
        marker = "  ← TODAY" if fc_date == run_date else ""
        log.info(box(f"  {str(fc_date):<12} {n_dist:>10,}  {max_hi:>7.1f}°C  {worst:<16}{marker}"))

    # Top 5 hôm nay
    log.info("╠" + "═" * (W - 2) + "╣")
    log.info(box(f"Top 5 District — HI cao nhất HÔM NAY ({run_date}):"))
    log.info(box(f"  {'District':<22} {'Temp':>7}  {'RH':>6}  {'HI':>8}  {'Level':<16}"))
    log.info(box("  " + "─" * 62))
    if top_today:
        for district, temp, rh, hi, level in top_today:
            name = (district or "Unknown")[:20]
            log.info(box(f"  {name:<22} {temp:>6.1f}°C  {rh:>5.0f}%  {hi:>7.1f}°C  {level:<16}"))
    else:
        log.info(box("  (Không có dữ liệu cho hôm nay)"))

    log.info("╚" + "═" * (W - 2) + "╝")


def run_stage2(
    conn:     psycopg2.extensions.connection,
    run_date: date,
) -> int:
    """
    Stage 2: Đọc forecast hôm nay → tính Heat Index → truncate today → insert alerts.
    Trả về tổng số rows đã inserted.
    """
    log.info("")
    log.info("━" * 60)
    log.info("  STAGE 2 — Heat Index Alert")
    log.info("━" * 60)
    log.info("[Stage 2] Thresholds (NOAA + Thai Meteorological Dept):")
    log.info("           NORMAL         HI < 27°C")
    log.info("           CAUTION        27 ≤ HI < 33°C")
    log.info("           WARNING        33 ≤ HI < 42°C")
    log.info("           DANGER         42 ≤ HI < 52°C")
    log.info("           EXTREME_DANGER HI ≥ 52°C")

    forecasts = fetch_forecasts_for_alert(conn, run_date)
    if not forecasts:
        log.warning("[Stage 2] Không có forecast data cho hôm nay — Stage 1 có thể thất bại.")
        return 0

    log.info(f"[Stage 2] {len(forecasts):,} records → tính Heat Index ...")

    alert_rows = build_alert_rows(forecasts)

    # Truncate dữ liệu hôm nay trước khi insert
    truncate_today(conn, "weather_alerts", run_date)
    insert_alerts(conn, alert_rows)

    log.info(f"[Stage 2] ✅ {len(alert_rows):,} alert records inserted.")
    print_alert_summary(conn, run_date)
    return len(alert_rows)


# ════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ════════════════════════════════════════════════════════════════════

def main() -> None:
    run_date = date.today()
    end_date = run_date + timedelta(days=FORECAST_DAYS - 1)

    log.info("=" * 60)
    log.info("  UNDP Meteorology — Unified Weather Pipeline")
    log.info(f"  Run date      : {run_date}")
    log.info(f"  Forecast range: {run_date} → {end_date}  ({FORECAST_DAYS} ngày)")
    log.info(f"  Model         : Open-Meteo Best Match  |  Resolution: Daily")
    log.info("=" * 60)

    # ── Kết nối DB ──────────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} ...")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công.")

    try:
        # ── Khởi tạo bảng ───────────────────────────────────────
        ensure_tables(conn)

        # ── Stage 1: Fetch Forecast ──────────────────────────────
        n_forecast = run_stage1(conn, run_date)

        if n_forecast == 0:
            log.error("[Pipeline] Stage 1 không insert được row nào — dừng pipeline.")
            return

        # ── Stage 2: Heat Index Alert ────────────────────────────
        n_alerts = run_stage2(conn, run_date)

        # ── Final Summary ────────────────────────────────────────
        log.info("")
        log.info("=" * 60)
        log.info("  PIPELINE HOÀN TẤT")
        log.info(f"  Forecast rows  : {n_forecast:,}")
        log.info(f"  Alert rows     : {n_alerts:,}")
        log.info(f"  Run date       : {run_date}")
        log.info("=" * 60)

    except Exception as e:
        log.error(f"[Pipeline] Lỗi nghiêm trọng: {e}", exc_info=True)
        conn.rollback()
        raise

    finally:
        conn.close()
        log.info("[DB] Connection closed.")


if __name__ == "__main__":
    main()