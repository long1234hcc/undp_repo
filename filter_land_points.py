"""
filter_land_points.py
─────────────────────
Lọc các điểm grid nằm ngoài đất liền (biển, đại dương) trước khi trả về API.
"""

import geopandas as gpd
from shapely.geometry import Point
import pandas as pd
import geodatasets
from shapely.ops import unary_union

# ── Load world shapefile một lần khi module được import ──────────────────────
try:
    # SỬ DỤNG CHÍNH XÁC TÊN DATASET: 'naturalearth.land'
    path = geodatasets.get_path('naturalearth.land') 
    _world = gpd.read_file(path)
    
    # Gộp tất cả polygon đất liền thành 1 polygon duy nhất
    _land_union = unary_union(_world.geometry.values)
    print("✓ Đã load thành công bản đồ đất liền.")
except Exception as e:
    print(f"⚠ Lỗi khi tải bản đồ: {e}")
    _land_union = None

def filter_to_land(data: list[dict]) -> list[dict]:
    if not data or _land_union is None:
        return data
    
    df = pd.DataFrame(data)
    geometry = [Point(float(row['lon']), float(row['lat'])) for row in data]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs='EPSG:4326')
    
    # Chỉ giữ các điểm chạm vào đất liền
    land_mask = gdf.geometry.intersects(_land_union) 
    filtered = gdf[land_mask].drop(columns='geometry')
    
    return filtered.to_dict('records')


# --- Test code ---
if __name__ == "__main__":
    test_data = [
        {"lon": 105.0, "lat": 15.0, "temp": 28.5}, # Đất liền (Việt Nam)
        {"lon": 110.0, "lat": 15.0, "temp": 29.0}, # Biển Đông (Loại)
        {"lon": 100.0, "lat": 20.0, "temp": 27.5}, # Đất liền (Thái Lan)
        {"lon": 108.0, "lat": 12.0, "temp": 30.0}, # Đất liền (Nam Việt Nam)
        {"lon": 112.0, "lat": 10.0, "temp": 29.5}, # Biển Đông (Loại)
        {"lon": 102.0, "lat": 2.0, "temp": 31.0},  # Đất liền (Gần Singapore)
    ]
    
    print("\nDữ liệu gốc:")
    for d in test_data: print(d)
    
    filtered_data = filter_to_land(test_data)
    
    print("\nDữ liệu sau khi lọc (chỉ đất liền):")
    for d in filtered_data: print(d)