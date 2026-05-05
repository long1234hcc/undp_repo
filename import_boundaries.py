"""
import_boundaries.py
────────────────────────────────────────────────────────────────
Mục đích:
  Đọc sea_level1_merged.geojson → insert vào bảng admin_polygons
  Dùng PostGIS để lưu geometry dạng MULTIPOLYGON, SRID 4326

Yêu cầu:
  pip install psycopg2-binary shapely
"""

import json
import logging
import psycopg2
from psycopg2.extras import execute_values
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
import os

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

GEOJSON_PATH = "./boundaries/sea_level1_merged.geojson"


def to_multipolygon_wkt(geometry: dict) -> str:
    """
    Chuyển GeoJSON geometry → WKT MULTIPOLYGON.
    GADM trả về cả Polygon lẫn MultiPolygon → chuẩn hóa hết về MultiPolygon
    để schema nhất quán.
    """
    geom = shape(geometry)

    # Nếu là Polygon đơn → wrap thành MultiPolygon
    if geom.geom_type == "Polygon":
        from shapely.geometry import MultiPolygon
        geom = MultiPolygon([geom])
    elif geom.geom_type == "MultiPolygon":
        pass
    else:
        # GeometryCollection hoặc type lạ → unary_union để normalize
        geom = unary_union(geom)

    return geom.wkt


def main():
    log.info("=" * 55)
    log.info("  Import SEA Boundaries → admin_polygons")
    log.info("=" * 55)

    # ── 1. Đọc GeoJSON ───────────────────────────────────────
    log.info(f"[Read] {GEOJSON_PATH} ...")
    with open(GEOJSON_PATH, encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson["features"]
    log.info(f"[Read] {len(features)} features")

    # ── 2. Kết nối DB ────────────────────────────────────────
    log.info(f"[DB] Kết nối {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}")
    conn = psycopg2.connect(**DB_CONFIG)
    log.info("[DB] Kết nối thành công")

    # ── 3. Truncate để idempotent ─────────────────────────────
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE admin_polygons RESTART IDENTITY CASCADE")
    conn.commit()
    log.info("[DB] Truncated admin_polygons")

    # ── 4. Parse + Insert ─────────────────────────────────────
    rows = []
    skipped = 0

    for feat in features:
        props    = feat.get("properties", {})
        geometry = feat.get("geometry")

        gid_1         = props.get("gid_1", "")
        province_name = props.get("province_name", "")
        country_name  = props.get("country_name", "")
        country_code  = props.get("country_code", "")
        type_         = props.get("type", "")

        # Skip nếu thiếu geometry hoặc gid_1
        if not geometry or not gid_1:
            log.warning(f"  [Skip] Thiếu geometry hoặc gid_1: {province_name}")
            skipped += 1
            continue

        try:
            wkt = to_multipolygon_wkt(geometry)
        except Exception as e:
            log.warning(f"  [Skip] Lỗi parse geometry {province_name}: {e}")
            skipped += 1
            continue

        rows.append((
            gid_1,
            province_name,
            country_name,
            country_code,
            type_,
            wkt,
        ))

    log.info(f"[Parse] {len(rows)} rows hợp lệ, {skipped} skipped")

    # ── 5. Bulk insert dùng ST_GeomFromText ──────────────────
    with conn.cursor() as cur:
        for row in rows:
            cur.execute("""
                INSERT INTO admin_polygons
                    (gid_1, province_name, country_name, country_code, type, geom)
                VALUES (%s, %s, %s, %s, %s,
                        ST_Multi(ST_GeomFromText(%s, 4326)))
                ON CONFLICT (gid_1) DO NOTHING
            """, row)

    conn.commit()
    log.info(f"[DB] Inserted {len(rows)} rows vào admin_polygons")

    # ── 6. Verify ─────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM admin_polygons")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT country_code, COUNT(*) as n
            FROM admin_polygons
            GROUP BY country_code
            ORDER BY country_code
        """)
        by_country = cur.fetchall()

    log.info(f"\n[Verify] Tổng: {total} provinces")
    log.info("-" * 35)
    for code, n in by_country:
        log.info(f"  {code:<6} {n:>3} provinces")

    conn.close()
    log.info("\n[Done] Import hoàn tất!")


if __name__ == "__main__":
    main()