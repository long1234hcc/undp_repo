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