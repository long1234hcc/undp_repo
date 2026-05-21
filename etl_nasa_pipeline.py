"""
UNDP HLS MASTER PIPELINE (2025)
Luồng: Quét API NASA -> Check DB Tracking -> Tải 3 Bands -> Tính NDVI/LST in RAM -> Lọc Mây -> Insert DB -> Xóa TIF
"""
import os
import sys
import re
import warnings
import urllib.parse
import numpy as np
import rasterio
from pyproj import Transformer
import psycopg2
from psycopg2.extras import execute_values
from datetime import datetime, timezone
import requests

warnings.filterwarnings("ignore")

# ==========================================
# CẤU HÌNH HỆ THỐNG
# ==========================================
NASA_USER = "lordnanao123"
NASA_PASS = "Lordnguyen1234@"
SAVE_DIR = "earthdata_downloads"

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "localhost"),
    "port":     int(os.getenv("DB_PORT", 5433)),
    "dbname":   os.getenv("DB_NAME", "undp_db"),
    "user":     os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "secretpassword"),
}

# Bounding box dự án
LAT_MIN, LAT_MAX = 13.5, 14.0
LON_MIN, LON_MAX = 100.3, 100.9

os.makedirs(SAVE_DIR, exist_ok=True)

# ==========================================
# 1. CLASS AUTH CỦA NASA
# ==========================================
class NasaSession(requests.Session):
    def __init__(self, username, password):
        super().__init__()
        self.auth = (username, password)

    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        url = prepared_request.url
        if 'Authorization' in headers:
            original = urllib.parse.urlparse(response.request.url).hostname
            redirect = urllib.parse.urlparse(url).hostname
            if original != redirect and redirect != 'urs.earthdata.nasa.gov':
                del headers['Authorization']
        return

# ==========================================
# 2. HÀM TÌM KIẾM THEO NĂM (ĐÃ FIX CHO 2025)
# ==========================================
def get_nasa_scenes_2025():
    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"
    params = {
        "short_name": "HLSL30",
        "version": "2.0",
        # --- LẤY TRỌN NĂM 2025 ---
        "temporal": "2025-01-01T00:00:00Z,2025-12-31T23:59:59Z", 
        "bounding_box": f"{LON_MIN},{LAT_MIN},{LON_MAX},{LAT_MAX}",
        # --- LẤY ẢNH MÂY <= 50% ---
        "cloud_cover": "0,50", 
        # --- VÉT SẠCH TRONG 1 LẦN GỌI ---
        "page_size": 2000      
    }
    
    print("[*] Đang quét danh bạ NASA cho năm 2025...")
    response = requests.get(cmr_url, params=params)
    if not response.ok:
        print(f"[-] Lỗi API CMR: {response.status_code}")
        return {}

    entries = response.json()['feed']['entry']
    target_bands = ('B04.tif', 'B05.tif', 'B10.tif') 
    
    # Gom nhóm link tải theo Scene_ID (1 Scene có 3 file)
    scenes_dict = {}
    
    for entry in entries:
        for link in entry.get('links', []):
            href = link.get('href', '')
            if 'data' in link.get('rel', '') and href.endswith(target_bands):
                # Lấy tên file gốc làm Key (vd: HLS.L30.T47PQR.2025002T033813.v2.0)
                filename = href.split('/')[-1]
                base_name = filename.replace('.B04.tif', '').replace('.B05.tif', '').replace('.B10.tif', '')
                
                if base_name not in scenes_dict:
                    scenes_dict[base_name] = []
                scenes_dict[base_name].append(href)
                
    return scenes_dict

# ==========================================
# 3. CÁC HÀM XỬ LÝ DATA VÀ ĐẨY DB (Từ Step trước)
# ==========================================
def extract_observation_time(scene_id):
    match = re.search(r'\.(\d{4})(\d{3})T(\d{6})\.', scene_id)
    if not match: return None
    year, julian_day, time_str = match.groups()
    dt = datetime.strptime(f"{year}{julian_day}{time_str}", "%Y%j%H%M%S")
    return dt.replace(tzinfo=timezone.utc)

def process_hls_to_memory(base_filename):
    b04_file = f"{base_filename}.B04.tif"
    b05_file = f"{base_filename}.B05.tif"
    b10_file = f"{base_filename}.B10.tif"

    scene_id = os.path.basename(base_filename)
    observed_at = extract_observation_time(scene_id)

    with rasterio.open(b04_file) as src_b04, \
         rasterio.open(b05_file) as src_b05, \
         rasterio.open(b10_file) as src_b10:
        red = src_b04.read(1).astype(np.float32).ravel()
        nir = src_b05.read(1).astype(np.float32).ravel()
        thermal = src_b10.read(1).astype(np.float32).ravel()
        crs, transform, width, height = src_b04.crs, src_b04.transform, src_b04.width, src_b04.height

    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    xs, ys = rasterio.transform.xy(transform, rows.ravel(), cols.ravel())
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = transformer.transform(np.array(xs), np.array(ys))

    mask = (lats >= LAT_MIN) & (lats <= LAT_MAX) & (lons >= LON_MIN) & (lons <= LON_MAX) & \
           (red != -9999) & (nir != -9999) & (thermal != -9999)

    pixel_count = mask.sum()
    if pixel_count == 0: return []

    red_valid, nir_valid, thermal_valid = red[mask], nir[mask], thermal[mask]
    lats_valid, lons_valid = lats[mask], lons[mask]

    ndvi = ((nir_valid * 0.0001) - (red_valid * 0.0001)) / ((nir_valid * 0.0001) + (red_valid * 0.0001) + 1e-8)
    lst_celsius = thermal_valid * 0.01

    records = []
    for i in range(int(pixel_count)):
        temp_c = round(float(lst_celsius[i]), 2)
        if temp_c < 10.0: continue # LỌC MÂY
        
        lat_val, lon_val = round(float(lats_valid[i]), 5), round(float(lons_valid[i]), 5)
        records.append((
            scene_id, observed_at, lat_val, lon_val, round(float(ndvi[i]), 4), temp_c,
            f"SRID=4326;POINT({lon_val} {lat_val})"
        ))
    return records

# ==========================================
# 4. CHẠY MASTER PIPELINE THỰC TẾ
# ==========================================
if __name__ == "__main__":
    print("="*60)
    print(" KHỞI ĐỘNG HLS MASTER PIPELINE - NĂM 2025")
    print("="*60)

    # 1. Kết nối DB
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        print("[+] Kết nối Database thành công!")
    except Exception as e:
        print(f"[-] Lỗi kết nối DB: {e}")
        sys.exit(1)

    # 2. Lấy danh sách Scene cần tải cho 2025
    scenes_dict = get_nasa_scenes_2025()
    total_scenes = len(scenes_dict)
    print(f"[+] Tìm thấy tổng cộng {total_scenes} Scenes phù hợp trong năm 2025.")

    session = NasaSession(NASA_USER, NASA_PASS)

    # 3. Vòng lặp Xử lý từng Scene
    for i, (scene_id, urls) in enumerate(scenes_dict.items(), 1):
        print(f"\n[{i}/{total_scenes}] XỬ LÝ SCENE: {scene_id}")
        
        # --- CHECK DB: NẾU ĐÃ XỬ LÝ RỒI THÌ BỎ QUA ---
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM hls_tracking_logs WHERE scene_id = %s", (scene_id,))
            res = cur.fetchone()
            if res and res[0] == 'SUCCESS':
                print("  -> Đã xử lý thành công trước đó. Bỏ qua.")
                continue
                
        # Ghi log bắt đầu xử lý vào DB
        obs_time = extract_observation_time(scene_id)
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO hls_tracking_logs (scene_id, capture_date, status) 
                VALUES (%s, %s, 'PROCESSING') 
                ON CONFLICT (scene_id) DO UPDATE SET status = 'PROCESSING'
            """, (scene_id, obs_time.date() if obs_time else None))
        conn.commit()

        # --- TẢI 3 FILES TIF ---
        download_success = True
        base_path = os.path.join(SAVE_DIR, scene_id)
        
        for url in urls:
            filename = url.split("/")[-1]
            save_path = os.path.join(SAVE_DIR, filename)
            
            if not os.path.exists(save_path):
                print(f"  -> Đang tải: {filename}...")
                resp = session.get(url, stream=True)
                if resp.ok:
                    with open(save_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=1024*1024): f.write(chunk)
                else:
                    print(f"  [-] Lỗi tải file: {resp.status_code}")
                    download_success = False
                    break
                    
        if not download_success:
            with conn.cursor() as cur:
                cur.execute("UPDATE hls_tracking_logs SET status = 'ERROR_NETWORK' WHERE scene_id = %s", (scene_id,))
            conn.commit()
            continue

        # --- XỬ LÝ DATA (RAM) VÀ ĐẨY DB ---
        data_records = process_hls_to_memory(base_path)
        
        if data_records is not None:
            if data_records:
                print(f"  -> Insert {len(data_records):,} điểm vào DB...")
                try:
                    with conn.cursor() as cur:
                        execute_values(
                            cur,
                            """
                            INSERT INTO hls_data_points (scene_id, observed_at, lat, lon, ndvi, lst_celsius, geom) 
                            VALUES %s 
                            ON CONFLICT (scene_id, lat, lon) DO NOTHING
                            """,
                            data_records, page_size=2000
                        )
                    db_status = 'SUCCESS'
                except Exception as e:
                    print(f"  [-] Lỗi DB: {e}")
                    db_status = 'ERROR_DB'
                    conn.rollback()
            else:
                print("  -> Scene rỗng (Bị mây che 100% khu vực). Bỏ qua Insert.")
                db_status = 'SUCCESS' # Xử lý thành công, chỉ là ko có data

            # Cập nhật trạng thái cuối cùng
            with conn.cursor() as cur:
                cur.execute("UPDATE hls_tracking_logs SET status = %s WHERE scene_id = %s", (db_status, scene_id))
            conn.commit()

        else:
            print("  [-] Dữ liệu tải về bị thiếu Band hoặc hỏng. Đánh dấu lỗi ERROR_MISSING_BANDS.")
            with conn.cursor() as cur:
                cur.execute("UPDATE hls_tracking_logs SET status = 'ERROR_MISSING_BANDS' WHERE scene_id = %s", (scene_id,))
            conn.commit()

        # --- DỌN DẸP Ổ CỨNG ---
        for ext in [".B04.tif", ".B05.tif", ".B10.tif"]:
            file_to_del = f"{base_path}{ext}"
            if os.path.exists(file_to_del): os.remove(file_to_del)
        print("  -> Đã xóa các file TIF tạm thời.")

    conn.close()
    print("\n[+] HOÀN TẤT TOÀN BỘ DATA PIPELINE NĂM 2025!")