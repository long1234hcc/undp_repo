"""
api.py
────────────────────────────────────────────────────────────────
Mục đích: Phục vụ dữ liệu thời tiết đã chuẩn hóa cho Frontend
Chạy server: uvicorn api:app --reload --port 8000
────────────────────────────────────────────────────────────────
"""

import os
from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Import hàm lọc từ file filter_land_points.py bạn vừa tạo
from filter_land_points import filter_to_land

load_dotenv()

app = FastAPI(title="UNDP Weather API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Cấu hình DB Pool ──────────────────────────────────────────
try:
    db_pool = SimpleConnectionPool(
        1, 10,
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", 5433),
        dbname=os.getenv("DB_NAME", "undp_db"),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "secretpassword")
    )
    print("✓ Đã kết nối Database thành công")
except Exception as e:
    print("⚠ Không thể kết nối DB:", e)
    db_pool = None


def get_db_connection():
    """Dependency chuẩn của FastAPI để quản lý connection"""
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database connection pool not initialized")
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)


# ── Endpoints ─────────────────────────────────────────────────

@app.get("/api/times")
def get_available_times(conn = Depends(get_db_connection)):
    """Trả về danh sách các mốc thời gian có trong DB, sắp xếp tăng dần."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT observed_at 
                FROM weather_observations 
                ORDER BY observed_at ASC;
            """)
            rows = cur.fetchall()
            times = [row[0].strftime("%Y-%m-%dT%H:%M:%SZ") for row in rows]
            return {"times": times}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/weather-grid")
def get_weather_grid(
    time: str = Query(..., description="ISO 8601 UTC Time, e.g. 2026-04-01T00:00:00Z"),
    conn = Depends(get_db_connection)
):
    """
    Trả về data grid theo giờ, đã lọc qua GeoPandas để lấy điểm trên đất liền.
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT 
                    lat_center AS lat, 
                    lon_center AS lon, 
                    temp_nor   AS "nT", 
                    humidity_nor AS "nH"
                FROM weather_observations
                WHERE observed_at = %s;
            """, (time,))

            rows = cur.fetchall()
            if not rows:
                raise HTTPException(status_code=404, detail=f"Không tìm thấy data cho giờ {time}")

            # Chuyển RealDictRow của psycopg2 thành list of dicts chuẩn
            raw_data = [dict(row) for row in rows]

            # ── Gọi hàm lọc điểm đất liền từ file filter_land_points ──
            land_rows = filter_to_land(raw_data)

            return land_rows

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))







# ══════════════════════════════════════════════════════════════
# NEW — Heatwave Alert Endpoints
# ══════════════════════════════════════════════════════════════
 
@app.get("/api/heatwave/dates")
def get_heatwave_dates(conn=Depends(get_db_connection)):
    """
    Trả về danh sách các forecast_run_date có alert,
    kèm summary (số district alert, max temp) cho mỗi ngày run.
 
    Response:
    {
      "dates": [
        {
          "run_date": "2026-05-18",
          "forecast_dates": ["2026-05-18", "2026-05-19", ...],
          "total_alerts": 43,
          "max_temp": 36.5
        },
        ...
      ],
      "latest_run_date": "2026-05-18"
    }
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    forecast_run_date::text                         AS run_date,
                    ARRAY_AGG(DISTINCT forecast_date::text
                              ORDER BY forecast_date::text)         AS forecast_dates,
                    COUNT(*)                                        AS total_alerts,
                    ROUND(MAX(temperature_2m)::numeric, 2)         AS max_temp
                FROM heatwave_alerts
                GROUP BY forecast_run_date
                ORDER BY forecast_run_date DESC
                LIMIT 30;
            """)
            rows = cur.fetchall()
 
        if not rows:
            return {"dates": [], "latest_run_date": None}
 
        return {
            "dates": [dict(r) for r in rows],
            "latest_run_date": rows[0]["run_date"],
        }
 
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
 
 
@app.get("/api/heatwave/alerts")
def get_heatwave_alerts(
    run_date: str = Query(..., description="forecast_run_date, e.g. 2026-05-18"),
    forecast_date: str = Query(None,  description="Lọc theo ngày dự báo cụ thể (optional)"),
    conn=Depends(get_db_connection)
):
    """
    Trả về danh sách alert kèm GeoJSON polygon của từng district.
 
    Response:
    {
      "type": "FeatureCollection",
      "features": [
        {
          "type": "Feature",
          "geometry": { GeoJSON polygon },
          "properties": {
            "gid_2": "...",
            "district_name": "...",
            "forecast_date": "2026-05-18",
            "temperature_2m": 36.5,
            "alert_level": "WARNING",
            "threshold": 35.0
          }
        },
        ...
      ],
      "meta": {
        "run_date": "2026-05-18",
        "total_features": 43,
        "max_temp": 36.5,
        "min_temp": 30.1
      }
    }
    """
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Build query — join heatwave_alerts với admin_polygons_district để lấy geometry
            query = """
                SELECT
                    ha.gid_1,
                    ha.gid_2,
                    ha.district_name,
                    ha.forecast_run_date::text              AS forecast_run_date,
                    ha.forecast_date::text                  AS forecast_date,
                    ROUND(ha.temperature_2m::numeric, 2)    AS temperature_2m,
                    ha.alert_level,
                    ha.threshold,
                    ST_AsGeoJSON(
                        ST_SimplifyPreserveTopology(apd.geom, 0.001)
                    )::json                                 AS geometry
                FROM heatwave_alerts ha
                JOIN admin_polygons_district apd
                    ON ha.gid_2 = apd.gid_2
                WHERE ha.forecast_run_date = %s
            """
            params = [run_date]
 
            if forecast_date:
                query += " AND ha.forecast_date = %s"
                params.append(forecast_date)
 
            query += " ORDER BY ha.temperature_2m DESC;"
 
            cur.execute(query, params)
            rows = cur.fetchall()
 
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"Không có alert nào cho run_date={run_date}"
                       + (f", forecast_date={forecast_date}" if forecast_date else "")
            )
 
        # Build GeoJSON FeatureCollection
        features = []
        temps = []
        for row in rows:
            d = dict(row)
            geom = d.pop("geometry")
            temps.append(float(d["temperature_2m"]))
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": d,
            })
 
        return {
            "type": "FeatureCollection",
            "features": features,
            "meta": {
                "run_date": run_date,
                "forecast_date": forecast_date,
                "total_features": len(features),
                "max_temp": round(max(temps), 2) if temps else None,
                "min_temp": round(min(temps), 2) if temps else None,
            },
        }
 
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
 