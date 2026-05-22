"""
UNDP Meteorology — Unified Weather Pipeline  (v2)
════════════════════════════════════════════════════════════════════════
Pipeline duy nhất thực hiện 1 stage:

  STAGE 1 — Forecast + Alert (gộp)
    • Lấy centroid tất cả districts Thailand từ bảng admin_polygons_district
    • Gọi Open-Meteo Forecast API (daily, 4 ngày: hôm nay + 3 ngày tới)
    • Tính mean = (max + min) / 2 cho temperature & relative_humidity
    • Tính Heat Index theo công thức Rothfusz / NOAA
    • Gán alert_level theo 5 mức (NOAA + Thai Meteorological Dept)
    • LEFT JOIN pop_district (ON gid_2 = district_code) → population, density
    • Tính percen_previous:
        forecast_date = run_date  →  0.0  (baseline, không có ngày trước)
        forecast_date = run_date+N →  (HI[N] - HI[N-1]) / HI[N-1] × 100
    • TRUNCATE forecast_run_date = today → INSERT weather_alerts

════════════════════════════════════════════════════════════════════════
Schema: weather_alerts  (bảng duy nhất)
  id                    SERIAL PRIMARY KEY
  gid_1                 TEXT
  gid_2                 TEXT
  lat_center            NUMERIC
  lon_center            NUMERIC
  forecast_run_date     DATE        -- Ngày script chạy
  forecast_date         DATE        -- Ngày được dự báo
  temperature_2m        NUMERIC     -- Mean = (max + min) / 2  (°C)
  relative_humidity_2m  NUMERIC     -- Mean = (max + min) / 2  (%)
  heat_index            NUMERIC     -- Feels-like temperature (°C)
  alert_level           TEXT        -- NORMAL/CAUTION/WARNING/DANGER/EXTREME_DANGER
  population            NUMERIC     -- Dân số quận (từ pop_district)
  density               NUMERIC     -- Mật độ dân số (người/km²)
  percen_previous       NUMERIC     -- % thay đổi HI so với ngày forecast liền trước
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
from collections import defaultdict

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
BATCH_SIZE    = 50   # Số quận mỗi batch API call
REQUEST_DELAY = 40   # Giây nghỉ giữa các batch (tránh rate limit)

API_URL = "https://api.open-meteo.com/v1/forecast"

# ── Heat Index Thresholds ────────────────────────────────────────────
# Nguồn: NOAA Heat Index scale + Thai Meteorological Dept / BMA Bangkok
# Mỗi tuple: (lower_bound_inclusive, alert_level)
# Duyệt từ trên xuống — match đầu tiên thắng.
HI_THRESHOLDS = [
    (52.0, "EXTREME_DANGER"),  # HI ≥ 52°C
    (42.0, "DANGER"),          # 42 ≤ HI < 52°C
    (33.0, "WARNING"),         # 33 ≤ HI < 42°C
    (27.0, "CAUTION"),         # 27 ≤ HI < 33°C
    (0.0,  "NORMAL"),          # HI < 27°C
]


# ════════════════════════════════════════════════════════════════════
# UTILITIES
# ════════════════════════════════════════════════════════════════════

def compute_heat_index(t: float, rh: float) -> float:
    """
    Tính Heat Index (°C) theo công thức Rothfusz — chuẩn NOAA.

    Điều kiện áp dụng:
      - T ≥ 27°C (80°F)
      - RH ≥ 40%
    Nếu không thỏa → HI = T (nhiệt độ thực).
    """
    if t < 27.0 or rh < 40.0:
        return round(t, 2)

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

    # Adjustment NOAA: không khí rất khô
    if rh < 13.0 and 80.0 <= tf <= 112.0:
        adjustment = ((13.0 - rh) / 4.0) * ((17.0 - abs(tf - 95.0)) / 17.0) ** 0.5
        hi_f -= adjustment
    # Adjustment NOAA: nhiệt độ thấp + độ ẩm rất cao
    elif rh > 85.0 and 80.0 <= tf <= 87.0:
        adjustment = ((rh - 85.0) / 10.0) * ((87.0 - tf) / 5.0)
        hi_f += adjustment

    hi_c = (hi_f - 32.0) * 5.0 / 9.0
    return round(hi_c, 2)


def get_alert_level(heat_index: float) -> str:
    """Tra HI_THRESHOLDS từ cao xuống thấp, trả về alert_level đầu tiên khớp."""
    for threshold, level in HI_THRESHOLDS:
        if heat_index >= threshold:
            return level
    return "NORMAL"


def compute_percen_previous(hi_current: float, hi_previous: float | None) -> float:
    """
    Tính % thay đổi Heat Index so với ngày forecast liền trước.

    - Ngày đầu (forecast_date = run_date): hi_previous = None → trả về 0.0
    - hi_previous = 0: tránh chia 0 → trả về 0.0
    - Công thức: (hi_current - hi_previous) / hi_previous × 100
    """
    if hi_previous is None or hi_previous == 0.0:
        return 0.0
    return round((hi_current - hi_previous) / hi_previous * 100, 4)


# ════════════════════════════════════════════════════════════════════
# DATABASE HELPERS
# ════════════════════════════════════════════════════════════════════

def ensure_table(conn: psycopg2.extensions.connection) -> None:
    """Tạo bảng weather_alerts nếu chưa tồn tại."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_alerts (
                id                    SERIAL PRIMARY KEY,
                gid_1                 TEXT,
                gid_2                 TEXT,
                lat_center            NUMERIC,
                lon_center            NUMERIC,
                forecast_run_date     DATE,
                forecast_date         DATE,
                temperature_2m        NUMERIC,
                relative_humidity_2m  NUMERIC,
                heat_index            NUMERIC,
                alert_level           TEXT,
                population            NUMERIC,
                density               NUMERIC,
                percen_previous       NUMERIC,
                geom                  GEOMETRY,
                UNIQUE (gid_2, forecast_date, forecast_run_date)
            );
        """)
    conn.commit()
    log.info("[DB] Bảng weather_alerts đã sẵn sàng.")


def truncate_today(
    conn:     psycopg2.extensions.connection,
    run_date: date,
) -> None:
    """Xóa toàn bộ record có forecast_run_date = run_date."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM weather_alerts WHERE forecast_run_date = %s",
            (run_date,),
        )
        deleted = cur.rowcount
    conn.commit()
    log.info(f"[DB] Truncate weather_alerts (run_date={run_date}): xóa {deleted:,} rows.")


def load_population_map(conn: psycopg2.extensions.connection) -> dict[str, dict]:
    """
    Đọc toàn bộ pop_district → dict keyed by district_code.
    Trả về: { "THA.1.1_1": { "population": ..., "density": ... }, ... }
    """
    with conn.cursor() as cur:
        cur.execute("SELECT district_code, population, density FROM pop_district;")
        rows = cur.fetchall()

    pop_map = {
        r[0]: {"population": float(r[1]) if r[1] is not None else None,
               "density":    float(r[2]) if r[2] is not None else None}
        for r in rows
    }
    log.info(f"[DB] Loaded {len(pop_map):,} records từ pop_district.")
    return pop_map


def insert_alerts(
    conn: psycopg2.extensions.connection,
    rows: list[tuple],
) -> None:
    """Insert alert rows vào weather_alerts, upsert nếu conflict."""
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO weather_alerts
                (gid_1, gid_2, lat_center, lon_center,
                 forecast_run_date, forecast_date,
                 temperature_2m, relative_humidity_2m,
                 heat_index, alert_level,
                 population, density, percen_previous,geom)
            VALUES %s
            ON CONFLICT (gid_2, forecast_date, forecast_run_date)
            DO UPDATE SET
                temperature_2m       = EXCLUDED.temperature_2m,
                relative_humidity_2m = EXCLUDED.relative_humidity_2m,
                heat_index           = EXCLUDED.heat_index,
                alert_level          = EXCLUDED.alert_level,
                population           = EXCLUDED.population,
                density              = EXCLUDED.density,
                percen_previous      = EXCLUDED.percen_previous,
                geom                 = EXCLUDED.geom
            """,
            rows,
            page_size=1000,
        )
    conn.commit()


# ════════════════════════════════════════════════════════════════════
# STAGE 1 — FETCH + COMPUTE + INSERT
# ════════════════════════════════════════════════════════════════════

def get_district_centroids(conn: psycopg2.extensions.connection) -> list[dict]:
    """Lấy tọa độ centroid tất cả districts Thailand từ DB."""
    log.info("[Stage 1] Trích xuất centroid tất cả districts Thailand ...")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                g1.gid_1,
                g1.gid_2,
                g1.district_name,
                g1.province_name,
                ROUND(ST_Y(ST_Centroid(g1.geom))::numeric, 8) AS lat_center,
                ROUND(ST_X(ST_Centroid(g1.geom))::numeric, 8) AS lon_center,
                ST_AsText(g1.geom) AS geom
            FROM admin_polygons_district g1
            WHERE g1.country_code = 'THA'
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
            "geom":          r[6],
        }
        for r in rows
    ]
    log.info(f"[Stage 1] {len(points)} districts tìm thấy.")
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


def parse_and_enrich_batch(
    api_data:     list[dict],
    batch_points: list[dict],
    run_date:     date,
    pop_map:      dict[str, dict],
) -> list[tuple]:
    """
    Parse API response → tính HI, alert_level, percen_previous, join population.
    Trả về list tuples sẵn sàng INSERT vào weather_alerts.

    percen_previous logic:
      - Sắp xếp các forecast_date của mỗi gid_2 theo thứ tự tăng dần
      - Ngày đầu tiên (= run_date): percen_previous = 0.0
      - Các ngày tiếp theo: (HI[N] - HI[N-1]) / HI[N-1] × 100
    """
    if len(api_data) != len(batch_points):
        log.warning(
            f"[Stage 1] Mismatch: API={len(api_data)} objects "
            f"vs batch={len(batch_points)} điểm — bỏ qua batch này."
        )
        return []

    # ── Bước 1: Parse raw records (chưa tính percen_previous) ───
    # Structure: { gid_2: [ { forecast_date, hi, ...fields }, ... ] }
    per_district: dict[str, list[dict]] = defaultdict(list)

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

        # Population từ pop_map (LEFT JOIN: None nếu không tìm thấy)
        pop_info   = pop_map.get(point["gid_2"], {})
        population = pop_info.get("population")
        density    = pop_info.get("density")

        for t_str, t_max, t_min, h_max, h_min in zip(
            times, t_maxs, t_mins, h_maxs, h_mins
        ):
            temp_mean = (
                round((float(t_max) + float(t_min)) / 2, 4)
                if t_max is not None and t_min is not None else None
            )
            hum_mean = (
                round((float(h_max) + float(h_min)) / 2, 4)
                if h_max is not None and h_min is not None else None
            )

            hi = (
                compute_heat_index(temp_mean, hum_mean)
                if temp_mean is not None and hum_mean is not None
                else None
            )
            level = get_alert_level(hi) if hi is not None else None

            per_district[point["gid_2"]].append({
                "gid_1":               point["gid_1"],
                "gid_2":               point["gid_2"],
                "lat_center":          point["lat_center"],
                "lon_center":          point["lon_center"],
                "geom":                point["geom"],
                "forecast_date":       date.fromisoformat(t_str),
                "temperature_2m":      temp_mean,
                "relative_humidity_2m": hum_mean,
                "heat_index":          hi,
                "alert_level":         level,
                "population":          population,
                "density":             density,
            })

    # ── Bước 2: Tính percen_previous theo từng district ─────────
    rows: list[tuple] = []

    for gid_2, records in per_district.items():
        # Sort theo forecast_date tăng dần để tính đúng thứ tự
        records.sort(key=lambda r: r["forecast_date"])

        prev_hi: float | None = None

        for rec in records:
            percen = compute_percen_previous(rec["heat_index"], prev_hi)
            prev_hi = rec["heat_index"]

            rows.append((
                rec["gid_1"],
                rec["gid_2"],
                rec["lat_center"],
                rec["lon_center"],
                run_date,                    # forecast_run_date
                rec["forecast_date"],        # forecast_date
                rec["temperature_2m"],
                rec["relative_humidity_2m"],
                rec["heat_index"],
                rec["alert_level"],
                rec["population"],
                rec["density"],
                percen,   # percen_previous                                 
                rec["geom"],                 # geom
            ))

    return rows


def run_stage1(
    conn:     psycopg2.extensions.connection,
    run_date: date,
) -> int:
    """
    Stage 1: Fetch forecast → compute HI + alert + percen_previous
             → join population → truncate today → insert.
    Trả về tổng số rows đã inserted.
    """
    log.info("")
    log.info("━" * 60)
    log.info("  STAGE 1 — Forecast + Alert + Population")
    log.info("━" * 60)

    # Load population map 1 lần trước khi vào vòng lặp batch
    pop_map = load_population_map(conn)

    target_points = get_district_centroids(conn)
    if not target_points:
        log.error("[Stage 1] Không tìm thấy district nào. Kiểm tra bảng admin_polygons_district.")
        return 0

    n_batches  = (len(target_points) + BATCH_SIZE - 1) // BATCH_SIZE
    total_rows = 0
    all_rows: list[tuple] = []
    n_failed   = 0

    log.info(
        f"[Stage 1] {len(target_points)} quận × {FORECAST_DAYS} ngày "
        f"= {len(target_points) * FORECAST_DAYS:,} rows kỳ vọng"
    )
    log.info(f"[Stage 1] {n_batches} batch × {BATCH_SIZE} quận — delay {REQUEST_DELAY}s/batch")
    log.info(f"[Stage 1] Pop map: {len(pop_map):,} districts loaded")

    for i in range(0, len(target_points), BATCH_SIZE):
        batch     = target_points[i : i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1

        log.info(f"  Batch {batch_num:03d}/{n_batches} — {len(batch)} điểm ...")
        try:
            api_data = fetch_batch(batch)
            rows     = parse_and_enrich_batch(api_data, batch, run_date, pop_map)
            all_rows.extend(rows)
            log.info(f"    ✓ {len(rows):,} rows parsed")

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

        finally:
            if i + BATCH_SIZE < len(target_points):
                time.sleep(REQUEST_DELAY)

    # Truncate sau khi fetch xong toàn bộ → tránh mất data nếu API lỗi giữa chừng
    if all_rows:
        truncate_today(conn, run_date)
        insert_alerts(conn, all_rows)
        total_rows = len(all_rows)

    log.info(
        f"[Stage 1] ✅ Hoàn tất — {total_rows:,} rows inserted | "
        f"{n_failed}/{n_batches} batch thất bại"
    )
    return total_rows


# ════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════

def print_summary(conn: psycopg2.extensions.connection, run_date: date) -> None:
    """In summary sau khi insert xong."""
    with conn.cursor() as cur:

        # Breakdown theo alert_level
        cur.execute("""
            SELECT
                alert_level,
                COUNT(*)                                         AS n_records,
                ROUND(AVG(heat_index)::numeric,           2)    AS avg_hi,
                ROUND(MAX(heat_index)::numeric,           2)    AS max_hi,
                ROUND(AVG(temperature_2m)::numeric,       2)    AS avg_temp,
                ROUND(AVG(relative_humidity_2m)::numeric, 2)    AS avg_rh
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

        # Top 5 quận HI cao nhất hôm nay
        cur.execute("""
            SELECT
                gid_2,
                temperature_2m,
                relative_humidity_2m,
                heat_index,
                alert_level,
                population,
                percen_previous
            FROM weather_alerts
            WHERE forecast_run_date = %s
              AND forecast_date     = %s
            ORDER BY heat_index DESC
            LIMIT 5
        """, (run_date, run_date))
        top_today = cur.fetchall()

    W = 70

    def box(text: str) -> str:
        return f"║  {text:<{W - 4}}║"

    log.info("")
    log.info("╔" + "═" * (W - 2) + "╗")
    log.info(box("WEATHER ALERT SUMMARY  (Heat Index + Population)"))
    log.info(box(f"Run date  : {run_date}"))
    log.info(box("Formula   : NOAA Rothfusz  |  percen_previous = ΔHI% vs prev forecast day"))
    log.info("╠" + "═" * (W - 2) + "╣")

    log.info(box("Alert Level Breakdown:"))
    log.info(box(f"  {'Level':<16} {'Records':>7}  {'Avg HI':>8}  {'Max HI':>8}  {'Avg Temp':>9}  {'Avg RH':>7}"))
    log.info(box("  " + "─" * 60))
    for level, n, avg_hi, max_hi, avg_t, avg_rh in level_stats:
        log.info(box(
            f"  {level:<16} {n:>7,}  {avg_hi:>7.1f}°C  {max_hi:>7.1f}°C"
            f"  {avg_t:>8.1f}°C  {avg_rh:>6.1f}%"
        ))
    if not level_stats:
        log.info(box("  (Không có dữ liệu)"))

    log.info("╠" + "═" * (W - 2) + "╣")
    log.info(box("Theo ngày forecast:"))
    log.info(box(f"  {'Date':<12} {'Districts':>10}  {'Max HI':>8}  {'Worst Level':<16}"))
    log.info(box("  " + "─" * 52))
    for fc_date, n_dist, max_hi, worst in by_day:
        marker = "  ← TODAY" if fc_date == run_date else ""
        log.info(box(f"  {str(fc_date):<12} {n_dist:>10,}  {max_hi:>7.1f}°C  {worst:<16}{marker}"))

    log.info("╠" + "═" * (W - 2) + "╣")
    log.info(box(f"Top 5 District — HI cao nhất HÔM NAY ({run_date}):"))
    log.info(box(f"  {'GID_2':<16} {'Temp':>7}  {'RH':>6}  {'HI':>8}  {'Δ%':>7}  {'Pop':>10}  {'Level'}"))
    log.info(box("  " + "─" * 66))
    if top_today:
        for gid2, temp, rh, hi, level, pop, pct in top_today:
            pop_str = f"{pop:,.0f}" if pop is not None else "N/A"
            log.info(box(
                f"  {gid2:<16} {temp:>6.1f}°C  {rh:>5.0f}%  {hi:>7.1f}°C"
                f"  {pct:>6.1f}%  {pop_str:>10}  {level}"
            ))
    else:
        log.info(box("  (Không có dữ liệu cho hôm nay)"))

    log.info("╚" + "═" * (W - 2) + "╝")


# ════════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ════════════════════════════════════════════════════════════════════

def main() -> None:
    run_date = date.today()
    end_date = run_date + timedelta(days=FORECAST_DAYS - 1)

    log.info("=" * 60)
    log.info("  UNDP Meteorology — Unified Weather Pipeline  v2")
    log.info(f"  Run date      : {run_date}")
    log.info(f"  Forecast range: {run_date} → {end_date}  ({FORECAST_DAYS} ngày)")
    log.info(f"  Model         : Open-Meteo Best Match  |  Resolution: Daily")
    log.info(f"  Output table  : weather_alerts  (single table)")
    log.info("=" * 60)

    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']} ...")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công.")

    try:
        ensure_table(conn)

        n_alerts = run_stage1(conn, run_date)

        if n_alerts == 0:
            log.error("[Pipeline] Không insert được row nào — kiểm tra API và DB.")
            return

        print_summary(conn, run_date)

        log.info("")
        log.info("=" * 60)
        log.info("  PIPELINE HOÀN TẤT")
        log.info(f"  Alert rows inserted : {n_alerts:,}")
        log.info(f"  Run date            : {run_date}")
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