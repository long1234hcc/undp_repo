import pandas as pd
import psycopg2
import os
import json
from shapely import wkt
from shapely.geometry import mapping

# ═══════════════════════════════════════════════════════════════
# Hàm Transform Geometry (WKT -> GeoJSON String)
# ═══════════════════════════════════════════════════════════════
def transform_to_json_string(wkt_string):
    if pd.isna(wkt_string) or not wkt_string:
        return None
        
    try:
        # 1. Parse WKT string từ Database thành object Shapely
        geom = wkt.loads(wkt_string)
        geojson = mapping(geom)
        
        # 2. Logic hạ cấp: MultiPolygon -> Polygon
        if geojson.get("type") == "MultiPolygon":
            polygons = geojson.get("coordinates", [])
            if len(polygons) == 1:
                geojson = {
                    "type": "Polygon",
                    "coordinates": polygons[0]
                }
                
        # 3. Trả về String (chuỗi hóa JSON) cho nhẹ và nhanh theo yêu cầu
        return json.dumps(geojson)
        
    except Exception as e:
        print(f"[-] Lỗi parse: {e}")
        return None

# ═══════════════════════════════════════════════════════════════
# Main Pipeline
# ═══════════════════════════════════════════════════════════════
def main():
    # 1. Kết nối DB
    print("[*] Đang kết nối Database...")
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5433)),
        dbname=os.getenv("DB_NAME", "undp_db"),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "secretpassword")
    )

    # 2. Query Data (JOIN bảng thời tiết và bảng bản đồ)
    # Dùng ST_AsText(geom) để lấy định dạng WKT nạp vào Shapely
    query = """
SELECT 
            w.id,
            w.gid_1,
            w.gid_2,
            a.district_name,
            w.lat_center,
            w.lon_center,
            w.observed_at,
            w.temperature_2m,            -- Đã bỏ chữ _mean, khớp 100% với DB của bạn
            w.relative_humidity_2m,      -- Đã bỏ chữ _mean, khớp 100% với DB của bạn
            w.temp_nor,
            w.humidity_nor,
            ST_AsText(a.geom) AS geom_wkt
        FROM weather_observations w
        LEFT JOIN admin_polygons_district a 
            ON w.gid_2 = a.gid_2
    """
    
    print("[*] Đang kéo data từ Database (có thể mất khoảng 10-20 giây)...")
    df = pd.read_sql(query, conn)
    print(f"[+] Đã kéo thành công {len(df):,} dòng.")

    # 3. Apply Transform
    print("[*] Đang xử lý hạ cấp Geometry và chuyển thành String...")
    df['polygon_geom'] = df['geom_wkt'].apply(transform_to_json_string)
    
    # Xóa cột WKT tạm sau khi đã xử lý xong cho nhẹ RAM
    df = df.drop(columns=['geom_wkt'])

    # 4. Ép kiểu datetime sang chuỗi để file JSON không bị lỗi
    print("[*] Đang chuẩn hóa định dạng Date...")
    df['observed_at'] = df['observed_at'].astype(str)

    # 5. Xuất File JSON
    print("[*] Đang đóng gói ra file JSON...")
    # Không dùng indent để file nén chặt nhất có thể, tối ưu dung lượng cho FE
    df.to_json("weather_observations_transformed.json", orient="records", force_ascii=False)

    print("[+] Hoàn tất! File weather_observations_transformed.json đã sẵn sàng.")
    conn.close()

if __name__ == "__main__":
    main()