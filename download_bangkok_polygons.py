"""
download_thailand_districts.py
Import toàn bộ ~928 districts của Thailand vào admin_polygons_district.
Không filter, không hardcode GID_1. Đơn giản và đúng.
"""

import os, json, logging
import requests
import psycopg2
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5433)),
    "dbname":   os.getenv("DB_NAME", "undp_db"),
    "user":     os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "secretpassword"),
}

GADM_URL   = "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_THA_2.json"
LOCAL_FILE = "./boundaries/THA_level2.geojson"


def download_tha_level2() -> dict:
    os.makedirs("./boundaries", exist_ok=True)

    if os.path.exists(LOCAL_FILE):
        log.info(f"[Skip] File đã tồn tại → {LOCAL_FILE}")
        with open(LOCAL_FILE, encoding="utf-8") as f:
            return json.load(f)

    log.info(f"[Download] {GADM_URL} ...")
    with requests.get(GADM_URL, timeout=180, stream=True) as resp:
        resp.raise_for_status()
        with open(LOCAL_FILE, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

    with open(LOCAL_FILE, encoding="utf-8") as f:
        data = json.load(f)

    log.info(f"  ✓ {len(data.get('features', []))} features → {LOCAL_FILE}")
    return data


def create_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_polygons_district (
                id            SERIAL PRIMARY KEY,
                gid_2         TEXT UNIQUE NOT NULL,
                gid_1         TEXT NOT NULL,
                district_name TEXT,
                province_name TEXT,
                country_name  TEXT,
                country_code  TEXT,
                type          TEXT,
                geom          GEOMETRY(GEOMETRY, 4326)
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_apd_gid_1 ON admin_polygons_district(gid_1);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_apd_geom  ON admin_polygons_district USING GIST(geom);")
    conn.commit()
    log.info("[DB] Bảng admin_polygons_district sẵn sàng")


def insert_all_districts(conn, features: list[dict]) -> None:
    rows = []
    skipped = 0
    for f in features:
        p = f["properties"]
        if f.get("geometry") is None:
            log.warning(f"[Skip] {p.get('GID_2')} thiếu geometry")
            skipped += 1
            continue

        rows.append((
            p.get("GID_2", ""),
            p.get("GID_1", ""),
            p.get("NAME_2", ""),
            p.get("NAME_1", ""),
            p.get("COUNTRY", ""),
            p.get("GID_0", ""),
            p.get("TYPE_2", ""),
            json.dumps(f["geometry"]),
        ))

    if skipped:
        log.warning(f"[Insert] Bỏ qua {skipped} features thiếu geometry")

    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO admin_polygons_district
                (gid_2, gid_1, district_name, province_name,
                 country_name, country_code, type, geom)
            VALUES %s
            ON CONFLICT (gid_2) DO UPDATE SET
                gid_1         = EXCLUDED.gid_1,
                district_name = EXCLUDED.district_name,
                province_name = EXCLUDED.province_name,
                country_name  = EXCLUDED.country_name,
                country_code  = EXCLUDED.country_code,
                type          = EXCLUDED.type,
                geom          = EXCLUDED.geom
            """,
            rows,
            template="(%s, %s, %s, %s, %s, %s, %s, ST_GeomFromGeoJSON(%s))",
            page_size=100,
        )
    conn.commit()
    log.info(f"[DB] Inserted/updated {len(rows)} districts")


def verify(conn) -> None:
    with conn.cursor() as cur:
        # Tổng số
        cur.execute("SELECT COUNT(*) FROM admin_polygons_district")
        total = cur.fetchone()[0]

        # Breakdown theo province — tìm Bangkok luôn tại đây
        cur.execute("""
            SELECT gid_1, province_name, COUNT(*) as cnt, 
                   string_agg(DISTINCT type, ', ') as types
            FROM admin_polygons_district
            GROUP BY gid_1, province_name
            ORDER BY cnt DESC
            LIMIT 10
        """)
        top10 = cur.fetchall()

    log.info(f"[Verify] Tổng: {total} districts trong DB")
    log.info(f"  {'GID_1':<15} {'Province':<30} {'Count':>5}  Types")
    log.info(f"  {'-'*65}")
    for gid1, pname, cnt, types in top10:
        log.info(f"  {gid1:<15} {pname:<30} {cnt:>5}  {types}")


def main() -> None:
    log.info("=" * 55)
    log.info("  Thailand All Districts — Download & Import")
    log.info("=" * 55)

    geojson  = download_tha_level2()
    features = geojson.get("features", [])
    log.info(f"[Load] {len(features)} features từ file")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        create_table(conn)
        insert_all_districts(conn, features)
        verify(conn)  # verify sẽ show Bangkok tự động trong top 10
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log.info("[Done] Import hoàn tất!")
    log.info("")
    log.info("Sau đó assign district cho weather_observations:")
    log.info("""
  UPDATE weather_observations wo
  SET gid_2 = apd.gid_2
  FROM admin_polygons_district apd
  WHERE ST_Contains(
      apd.geom,
      ST_SetSRID(ST_Point(wo.lon_center, wo.lat_center), 4326)
  );
    """)


if __name__ == "__main__":
    main()