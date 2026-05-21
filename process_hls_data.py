"""
HLS Landsat TIF -> JSON (dùng rasterio + pyproj, tính sẵn NDVI & LST)
Chạy: python hls_to_json.py
"""
import os
import sys
import json
from turtle import width
import warnings
import numpy as np
import rasterio
from pyproj import Transformer

warnings.filterwarnings("ignore")

DEFAULT_OUT_DIR = "./output"

# Bounding box giống hệt cấu hình của bạn
LAT_MIN = 13.5
LAT_MAX = 14.0
LON_MIN = 100.3
LON_MAX = 100.9

def hls_to_json(base_filename, out_dir):
    # Khôi phục đường dẫn tới 3 file tif (Red, NIR, Thermal)
    # Giả sử file đang nằm cùng thư mục chạy lệnh, hoặc bạn chỉnh sửa lại đường dẫn nếu cần
    b04_file = f"{base_filename}.B04.tif"
    b05_file = f"{base_filename}.B05.tif"
    b10_file = f"{base_filename}.B10.tif"

    for f in [b04_file, b05_file, b10_file]:
        if not os.path.exists(f):
            print(f"❌ Không tìm thấy file: {f}")
            return

    print(f"[*] Đang đọc dữ liệu từ nhóm file: {base_filename}...")
    
    with rasterio.open(b04_file) as src_b04, \
            rasterio.open(b05_file) as src_b05, \
            rasterio.open(b10_file) as src_b10:
            
            # THÊM .ravel() ĐỂ DUỖI THẲNG MA TRẬN 2D THÀNH 1D
            red = src_b04.read(1).astype(np.float32).ravel()
            nir = src_b05.read(1).astype(np.float32).ravel()
            thermal = src_b10.read(1).astype(np.float32).ravel()

            # Lấy thông tin hệ tọa độ (CRS) và kích thước
            crs = src_b04.crs
            transform = src_b04.transform
            width, height = src_b04.width, src_b04.height

    print(f"[*] Đang chuyển đổi tọa độ từ {crs} sang EPSG:4326 (Lat/Lon)...")
    
    # Tạo lưới X, Y trong hệ tọa độ gốc (UTM)
    cols, rows = np.meshgrid(np.arange(width), np.arange(height))
    xs, ys = rasterio.transform.xy(transform, rows.ravel(), cols.ravel())
    
    xs = np.array(xs)
    ys = np.array(ys)

    # Convert sang Lat/Lon
    transformer = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lons, lats = transformer.transform(xs, ys)

    # --- BƯỚC QUAN TRỌNG: LỌC BBOX VÀ PIXEL LỖI (-9999) ---
    mask = (lats >= LAT_MIN) & (lats <= LAT_MAX) & \
           (lons >= LON_MIN) & (lons <= LON_MAX) & \
           (red != -9999) & (nir != -9999) & (thermal != -9999)

    pixel_count = mask.sum()
    print(f"[*] Pixels hợp lệ trong bbox: {pixel_count:,}")

    if pixel_count == 0:
        print("❌ Không có pixel nào lọt vào Bounding Box!")
        return

    # Trích xuất dữ liệu những pixel nằm trong mask
    red_valid = red[mask]
    nir_valid = nir[mask]
    thermal_valid = thermal[mask]
    
    lats_valid = lats[mask]
    lons_valid = lons[mask]

    print("[*] Đang tính toán NDVI và LST...")
    # Tính NDVI (Scale factor của Band quang học là 0.0001)
    red_scaled = red_valid * 0.0001
    nir_scaled = nir_valid * 0.0001
    # Cộng thêm 1e-8 để tránh lỗi ZeroDivisionError
    ndvi = (nir_scaled - red_scaled) / (nir_scaled + red_scaled + 1e-8)

    # Tính LST (Scale factor của Band nhiệt là 0.01, trừ 273.15 để ra độ C)
    lst_celsius = thermal_valid * 0.01

    print("[*] Đang đóng gói thành JSON...")
    # Parse ra list dict y hệt format của bạn
    records = []
    for i in range(int(pixel_count)):
        records.append({
            "lat": round(float(lats_valid[i]), 5),
            "lon": round(float(lons_valid[i]), 5),
            "ndvi": round(float(ndvi[i]), 4),
            "lst_celsius": round(float(lst_celsius[i]), 2)
        })

    output = {
        "metadata": {
            "source": base_filename,
            "bbox": {"lat_min": LAT_MIN, "lat_max": LAT_MAX, "lon_min": LON_MIN, "lon_max": LON_MAX},
            "total_rows": len(records),
        },
        "data": records,
    }

    # Lưu file
    # Lấy tên file gốc (bỏ đi phần thư mục earthdata_downloads)
    clean_name = os.path.basename(base_filename)
    
    # Lưu file
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{clean_name}.json") # Dùng clean_name ở đây
    with open(out_path, "w", encoding="utf-8") as jf:
        json.dump(output, jf, indent=2, ensure_ascii=False)

    print(f"✅ JSON saved → {out_path} ({os.path.getsize(out_path)//1024} KB)")
    
if __name__ == "__main__":
    import glob

    # Khai báo thư mục chứa data (file .tif) và thư mục lưu JSON
    DATA_DIR = "earthdata_downloads"
    os.makedirs(DEFAULT_OUT_DIR, exist_ok=True)
    
    # Dùng glob để tìm TẤT CẢ các file B04.tif trong thư mục tải về
    search_pattern = os.path.join(DATA_DIR, "*B04.tif")
    b04_files = glob.glob(search_pattern)
    
    if not b04_files:
        print(f"[-] Không tìm thấy file dữ liệu nào trong thư mục '{DATA_DIR}'")
        sys.exit(0)
    
    print(f"[*] Tìm thấy {len(b04_files)} scenes để xử lý.")
    
    # Xử lý lần lượt từng scene
    for b04_path in b04_files:
        # Ví dụ b04_path = "earthdata_downloads\HLS.L30.T47PQR.2023002T033813.v2.0.B04.tif"
        # Ta cắt bỏ đuôi ".B04.tif" để lấy ra được cái base name
        base_path = b04_path.replace(".B04.tif", "")
        
        print("-" * 50)
        hls_to_json(base_path, DEFAULT_OUT_DIR)
        
    print("=" * 50)
    print("[+] HOÀN TẤT QUÁ TRÌNH TRANSFORM TOÀN BỘ FILE!")