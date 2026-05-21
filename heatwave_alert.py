"""
UNDP Meteorology — Heatwave Alert Generator
────────────────────────────────────────────────────────────────
Mục đích:
  1. Đọc dữ liệu forecast từ bảng weather_forecasts (run_date = hôm nay).
  2. Filter các record có temperature_2m > TEMP_THRESHOLD (default 35°C).
  3. Insert alert vào bảng heatwave_alerts.
  4. In summary ra log.

Chạy SAU forecast_weather.py (cách 5 phút trong cron/Airflow).

Cron example:
  07:00  forecast_weather.py
  07:05  heatwave_alert.py
────────────────────────────────────────────────────────────────
Schema bảng heatwave_alerts (tự tạo nếu chưa có):
  id                SERIAL PRIMARY KEY
  gid_1             TEXT                  -- province level
  gid_2             TEXT                  -- district level
  district_name     TEXT
  forecast_run_date DATE                  -- ngày script chạy
  forecast_date     DATE                  -- ngày được dự báo nóng
  temperature_2m    NUMERIC               -- nhiệt độ dự báo (°C)
  threshold         NUMERIC DEFAULT 35    -- ngưỡng trigger
  alert_level       TEXT                  -- 'WARNING' / 'DANGER'
  created_at        TIMESTAMPTZ DEFAULT now()
  UNIQUE (gid_2, forecast_date, forecast_run_date)
────────────────────────────────────────────────────────────────
Alert Level:
  WARNING : 35°C <= temp < 38°C
  DANGER  : temp >= 38°C
────────────────────────────────────────────────────────────────
"""

import os
import logging
import psycopg2
from psycopg2.extras import execute_values
from datetime import date

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

TEMP_THRESHOLD    = float(os.getenv("HEATWAVE_THRESHOLD", 30.0))  # °C
DANGER_THRESHOLD  = float(os.getenv("HEATWAVE_DANGER",    35.0))  # °C


# ═══════════════════════════════════════════════════════════════
# Bước 1: Tạo bảng heatwave_alerts nếu chưa có
# ═══════════════════════════════════════════════════════════════
def ensure_table(conn: psycopg2.extensions.connection) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS heatwave_alerts (
                id                SERIAL PRIMARY KEY,
                gid_1             TEXT,
                gid_2             TEXT,
                district_name     TEXT,
                forecast_run_date DATE,
                forecast_date     DATE,
                temperature_2m    NUMERIC,
                threshold         NUMERIC DEFAULT 35,
                alert_level       TEXT,
                created_at        TIMESTAMPTZ DEFAULT now(),
                UNIQUE (gid_2, forecast_date, forecast_run_date)
            );
        """)
    conn.commit()
    log.info("[DB] Bảng heatwave_alerts đã sẵn sàng.")


# ═══════════════════════════════════════════════════════════════
# Bước 2: Đọc forecast hôm nay, filter theo ngưỡng nhiệt độ
# ═══════════════════════════════════════════════════════════════
def fetch_heatwave_forecasts(
    conn: psycopg2.extensions.connection,
    run_date: date,
) -> list[dict]:
    """
    Join weather_forecasts với admin_polygons_district để lấy district_name.
    Filter: forecast_run_date = hôm nay AND temperature_2m > TEMP_THRESHOLD.
    """
    log.info(f"[DB] Đọc forecast run_date={run_date}, filter temp > {TEMP_THRESHOLD}°C ...")

    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                wf.gid_1,
                wf.gid_2,
                apd.district_name,
                wf.forecast_run_date,
                wf.forecast_date,
                wf.temperature_2m
            FROM weather_forecasts wf
            LEFT JOIN admin_polygons_district apd
                ON wf.gid_2 = apd.gid_2
            WHERE wf.forecast_run_date = %s
              AND wf.temperature_2m    > %s
            ORDER BY wf.forecast_date, wf.temperature_2m DESC
        """, (run_date, TEMP_THRESHOLD))

        rows = cur.fetchall()

    results = []
    for row in rows:
        results.append({
            "gid_1":             row[0],
            "gid_2":             row[1],
            "district_name":     row[2],
            "forecast_run_date": row[3],
            "forecast_date":     row[4],
            "temperature_2m":    float(row[5]) if row[5] is not None else None,
        })

    log.info(f"[DB] Tìm thấy {len(results)} record vượt ngưỡng {TEMP_THRESHOLD}°C.")
    return results


# ═══════════════════════════════════════════════════════════════
# Bước 3: Xác định alert_level
# ═══════════════════════════════════════════════════════════════
def get_alert_level(temperature: float) -> str:
    """
    WARNING : 35°C <= temp < 38°C
    DANGER  : temp >= 38°C
    """
    if temperature >= DANGER_THRESHOLD:
        return "DANGER"
    return "WARNING"


# ═══════════════════════════════════════════════════════════════
# Bước 4: Insert vào heatwave_alerts
# ═══════════════════════════════════════════════════════════════
def insert_alerts(
    conn: psycopg2.extensions.connection,
    forecasts: list[dict],
) -> int:
    if not forecasts:
        log.info("[DB] Không có alert nào để insert.")
        return 0

    rows = []
    for f in forecasts:
        rows.append((
            f["gid_1"],
            f["gid_2"],
            f["district_name"],
            f["forecast_run_date"],
            f["forecast_date"],
            f["temperature_2m"],
            TEMP_THRESHOLD,
            get_alert_level(f["temperature_2m"]),
        ))

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO heatwave_alerts
                (gid_1, gid_2, district_name,
                 forecast_run_date, forecast_date,
                 temperature_2m, threshold, alert_level)
            VALUES %s
            ON CONFLICT (gid_2, forecast_date, forecast_run_date)
            DO UPDATE SET
                temperature_2m = EXCLUDED.temperature_2m,
                alert_level    = EXCLUDED.alert_level,
                created_at     = now()
            """,
            rows,
            page_size=500,
        )
    conn.commit()

    log.info(f"[DB] Inserted/Updated {len(rows)} alert records.")
    return len(rows)


# ═══════════════════════════════════════════════════════════════
# Bước 5: Log summary ra console
# ═══════════════════════════════════════════════════════════════
def print_summary(
    conn: psycopg2.extensions.connection,
    run_date: date,
) -> None:
    with conn.cursor() as cur:
        # Tổng alert hôm nay
        cur.execute("""
            SELECT
                alert_level,
                COUNT(*) AS cnt,
                ROUND(AVG(temperature_2m)::numeric, 2) AS avg_temp,
                ROUND(MAX(temperature_2m)::numeric, 2) AS max_temp
            FROM heatwave_alerts
            WHERE forecast_run_date = %s
            GROUP BY alert_level
            ORDER BY alert_level DESC
        """, (run_date,))
        level_stats = cur.fetchall()

        # Top 5 district nóng nhất (hôm nay)
        cur.execute("""
            SELECT
                district_name,
                forecast_date,
                temperature_2m,
                alert_level
            FROM heatwave_alerts
            WHERE forecast_run_date = %s
              AND forecast_date     = %s
            ORDER BY temperature_2m DESC
            LIMIT 5
        """, (run_date, run_date))
        top_today = cur.fetchall()

        # Breakdown theo ngày
        cur.execute("""
            SELECT
                forecast_date,
                COUNT(*) AS n_districts,
                ROUND(MAX(temperature_2m)::numeric, 2) AS max_temp
            FROM heatwave_alerts
            WHERE forecast_run_date = %s
            GROUP BY forecast_date
            ORDER BY forecast_date
        """, (run_date,))
        by_day = cur.fetchall()

    log.info("")
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║          HEATWAVE ALERT SUMMARY                     ║")
    log.info(f"║  Run date : {run_date}                              ║")
    log.info("╠══════════════════════════════════════════════════════╣")

    log.info("║  Alert Level Breakdown:                             ║")
    for level, cnt, avg_t, max_t in level_stats:
        log.info(f"║    {level:<10} → {cnt:>3} records | avg {avg_t}°C | max {max_t}°C  ║")

    if not level_stats:
        log.info("║    ✅ Không có district nào vượt ngưỡng hôm nay     ║")

    log.info("╠══════════════════════════════════════════════════════╣")
    log.info("║  Alert theo ngày:                                   ║")
    for fc_date, n_dist, max_t in by_day:
        label = " ← TODAY" if fc_date == run_date else ""
        log.info(f"║    {fc_date}  {n_dist:>3} districts  max {max_t}°C{label:<10}║")

    log.info("╠══════════════════════════════════════════════════════╣")
    log.info(f"║  Top 5 District nóng nhất HÔM NAY ({run_date}):   ║")
    if top_today:
        for district, fc_date, temp, level in top_today:
            name = (district or "Unknown")[:20]
            log.info(f"║    {name:<22} {temp:>5.1f}°C  [{level}]       ║")
    else:
        log.info("║    (Không có alert nào cho hôm nay)                 ║")

    log.info("╚══════════════════════════════════════════════════════╝")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════
def main() -> None:
    run_date = date.today()

    log.info("=" * 60)
    log.info("  UNDP Meteorology — Heatwave Alert Generator")
    log.info(f"  Run date  : {run_date}")
    log.info(f"  Threshold : WARNING >= {TEMP_THRESHOLD}°C  |  DANGER >= {DANGER_THRESHOLD}°C")
    log.info("=" * 60)

    # ── 1. Kết nối DB ────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công")

    # ── 2. Đảm bảo bảng tồn tại ──────────────────────────────
    ensure_table(conn)

    # ── 3. Đọc forecast + filter ─────────────────────────────
    forecasts = fetch_heatwave_forecasts(conn, run_date)

    if not forecasts:
        log.info("[Done] Không có heatwave alert nào cho hôm nay. Pipeline kết thúc.")
        conn.close()
        return

    # ── 4. Insert alerts ──────────────────────────────────────
    n_inserted = insert_alerts(conn, forecasts)

    # ── 5. Summary ───────────────────────────────────────────
    print_summary(conn, run_date)

    log.info("")
    log.info(f"[Done] Heatwave alert pipeline hoàn tất — {n_inserted} alerts ghi vào DB.")

    conn.close()
    log.info("[DB] Connection closed.")


if __name__ == "__main__":
    main()