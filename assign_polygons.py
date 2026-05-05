"""
assign_polygons.py
────────────────────────────────────────────────────────────────
Mục đích:
  Dùng PostGIS ST_Contains để gán gid_1 (tỉnh/bang) cho từng
  grid point trong bảng grid_points, sau đó propagate sang
  weather_observations.

  Logic:
    1. Với mỗi (lon, lat) trong grid_points:
       → Tìm polygon nào chứa điểm đó (ST_Contains)
       → Gán gid_1 của polygon đó vào grid_points.gid_1
    2. UPDATE weather_observations.gid_1
       từ grid_points (join theo lat/lon)

  Chạy 1 lần sau khi có đủ data.
"""

import os
import logging
import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5433)),
    "dbname":   os.getenv("DB_NAME", "undp_db"),
    "user":     os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "secretpassword"),
}


def main():
    log.info("=" * 55)
    log.info("  Assign Polygons → grid_points + weather_observations")
    log.info("=" * 55)

    conn = psycopg2.connect(**DB_CONFIG)

    # ── 1. Kiểm tra số grid points ───────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM grid_points")
        gp_total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM admin_polygons")
        poly_total = cur.fetchone()[0]

    log.info(f"[Info] grid_points   : {gp_total:,} rows")
    log.info(f"[Info] admin_polygons: {poly_total:,} rows")

    # ── 2. Gán gid_1 cho grid_points dùng ST_Contains ────────
    log.info("\n[Step 1] Gán gid_1 cho grid_points ...")
    log.info("  (Dùng PostGIS ST_Contains — có thể mất 10-30s)")

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE grid_points gp
            SET gid_1 = ap.gid_1
            FROM admin_polygons ap
            WHERE ST_Contains(
                ap.geom,
                ST_SetSRID(ST_Point(gp.lon_center, gp.lat_center), 4326)
            )
        """)
        updated_gp = cur.rowcount
    conn.commit()
    log.info(f"  ✓ {updated_gp:,} grid points được gán gid_1")

    # ── 3. Kiểm tra grid points nằm ngoài polygon (biển) ─────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM grid_points WHERE gid_1 IS NULL
        """)
        null_gp = cur.fetchone()[0]
    log.info(f"  ℹ {null_gp:,} grid points nằm ngoài polygon (biển/ocean) — bình thường")

    # ── 4. Propagate sang weather_observations ────────────────
    log.info("\n[Step 2] Propagate gid_1 → weather_observations ...")

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE weather_observations wo
            SET gid_1 = gp.gid_1
            FROM grid_points gp
            WHERE wo.lat_center = gp.lat_center
              AND wo.lon_center = gp.lon_center
        """)
        updated_wo = cur.rowcount
    conn.commit()
    log.info(f"  ✓ {updated_wo:,} weather_observations được gán gid_1")

    # ── 5. Verify ─────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(DISTINCT gid_1)
            FROM weather_observations
            WHERE gid_1 IS NOT NULL
        """)
        distinct_provinces = cur.fetchone()[0]

        cur.execute("""
            SELECT ap.country_code, COUNT(DISTINCT gp.gid_1) as provinces
            FROM grid_points gp
            JOIN admin_polygons ap ON gp.gid_1 = ap.gid_1
            GROUP BY ap.country_code
            ORDER BY ap.country_code
        """)
        by_country = cur.fetchall()

    log.info(f"\n[Verify] {distinct_provinces} provinces có grid points")
    log.info("-" * 35)
    for code, n in by_country:
        log.info(f"  {code:<6} {n:>3} provinces có data")

    conn.close()
    log.info("\n[Done] Assign polygons hoàn tất!")


if __name__ == "__main__":
    main()