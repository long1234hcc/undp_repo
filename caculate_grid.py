"""
Script: generate_sea_grid.py
Mục đích: Tính toàn bộ tọa độ grid 1°x1° bao phủ Đông Nam Á
Output: sea_grid_coordinates.csv + in ra màn hình summary

Bounding box Đông Nam Á:
  lat: -10.0 → 28.0  (Nam → Bắc)
  lon:  95.0 → 141.0 (Tây → Đông)

Mỗi điểm = CENTER của ô 1°x1°
  Ô (lat=10, lon=100) → center = (10.5, 100.5)
"""

import numpy as np
import csv
import os

# ── Bounding box ĐNA ──────────────────────────────────────────────────────────
LAT_MIN = -10.0
LAT_MAX =  28.0
LON_MIN =  95.0
LON_MAX = 141.0
STEP    =   0.2   # độ phân giải 1° x 1°

# ── Tính các cạnh ô (edges) rồi lấy center ───────────────────────────────────
# np.arange tạo ra các cạnh trái của từng ô
lat_edges = np.arange(LAT_MIN, LAT_MAX, STEP)   # [-10, -9, ..., 27]
lon_edges = np.arange(LON_MIN, LON_MAX, STEP)   # [95, 96, ..., 140]

# Center = edge + STEP/2
lat_centers = lat_edges + STEP / 2   # [-9.5, -8.5, ..., 27.5]
lon_centers = lon_edges + STEP / 2   # [95.5, 96.5, ..., 140.5]

# ── Tạo tất cả cặp (lat_center, lon_center) ──────────────────────────────────
grid_points = []
for lat in lat_centers:
    for lon in lon_centers:
        grid_points.append({
            "lat_center": round(float(lat), 1),
            "lon_center": round(float(lon), 1),
            "lat_edge_south": round(float(lat - STEP/2), 1),
            "lat_edge_north": round(float(lat + STEP/2), 1),
            "lon_edge_west":  round(float(lon - STEP/2), 1),
            "lon_edge_east":  round(float(lon + STEP/2), 1),
        })

# ── Summary ───────────────────────────────────────────────────────────────────
n_lat  = len(lat_centers)
n_lon  = len(lon_centers)
n_total = len(grid_points)

print("=" * 60)
print("  GRID ĐNA — 1° x 1°")
print("=" * 60)
print(f"  Bounding box  : lat [{LAT_MIN}, {LAT_MAX}] | lon [{LON_MIN}, {LON_MAX}]")
print(f"  Số hàng (lat) : {n_lat}  ({lat_centers[0]} → {lat_centers[-1]})")
print(f"  Số cột (lon)  : {n_lon}  ({lon_centers[0]} → {lon_centers[-1]})")
print(f"  Tổng điểm     : {n_lat} × {n_lon} = {n_total} điểm")
print(f"  Step          : {STEP}°  (~111 km mỗi ô)")
print()

# ── In 5 dòng đầu để kiểm tra ─────────────────────────────────────────────────
print("  5 điểm đầu tiên:")
print(f"  {'lat_center':>10} {'lon_center':>10} {'lat_S':>7} {'lat_N':>7} {'lon_W':>7} {'lon_E':>7}")
print("  " + "-" * 55)
for p in grid_points[:5]:
    print(f"  {p['lat_center']:>10} {p['lon_center']:>10} "
          f"{p['lat_edge_south']:>7} {p['lat_edge_north']:>7} "
          f"{p['lon_edge_west']:>7} {p['lon_edge_east']:>7}")

print()
print("  5 điểm cuối cùng:")
print(f"  {'lat_center':>10} {'lon_center':>10} {'lat_S':>7} {'lat_N':>7} {'lon_W':>7} {'lon_E':>7}")
print("  " + "-" * 55)
for p in grid_points[-5:]:
    print(f"  {p['lat_center']:>10} {p['lon_center']:>10} "
          f"{p['lat_edge_south']:>7} {p['lat_edge_north']:>7} "
          f"{p['lon_edge_west']:>7} {p['lon_edge_east']:>7}")

# ── Xuất CSV ──────────────────────────────────────────────────────────────────
output_path = os.path.join(os.path.dirname(__file__), "sea_grid_coordinates.csv")
fieldnames = ["lat_center", "lon_center",
              "lat_edge_south", "lat_edge_north",
              "lon_edge_west",  "lon_edge_east"]

with open(output_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(grid_points)

print()
print(f"  CSV đã lưu: {output_path}")
print("=" * 60)

# ── Export thêm 2 list phẳng để dùng trực tiếp trong API call ─────────────────
lat_list = [p["lat_center"] for p in grid_points]
lon_list = [p["lon_center"] for p in grid_points]

print()
print("  Dạng list để paste vào API call:")
print(f"  latitudes  = {lat_list[:5]} ... (total {len(lat_list)})")
print(f"  longitudes = {lon_list[:5]} ... (total {len(lon_list)})")