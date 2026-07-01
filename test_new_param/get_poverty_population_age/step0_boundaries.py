"""
step0_boundaries.py
===================
Bước 0: Đọc GADM boundary file cho Thailand và India,
extract admin level-1 (tỉnh/state), verify, xuất ra JSON.

Output:
    boundaries.json — list 112 record (77 tỉnh THA + 35 state IND)

Schema mỗi record:
    country        str   "Thailand" | "India"
    admin_code     str   mã hành chính từ GADM (GID_1)
    admin_name     str   tên tiếng Anh (NAME_1)
    centroid_lon   float kinh độ tâm tỉnh
    centroid_lat   float vĩ độ tâm tỉnh
    geometry       dict  GeoJSON polygon — dùng cho spatial join ở bước sau

Verify sau khi chạy:
    - Thailand: đúng 77 tỉnh
    - India   : đúng 35 state/union territory (Jammu & Kashmir và
                Ladakh không có trong gadm41 — xem ghi chú expected_count)
    - Không có geometry null
    - File boundaries.json tạo thành công
"""

import json
import os
import geopandas as gpd

# ── Config ────────────────────────────────────────────────────────────────────

BOUNDARIES_DIR = r"C:\Users\DELL\Desktop\UNDP\boundaries"
OUTPUT_JSON    = "boundaries.json"

COUNTRIES = {
    "Thailand": {
        "gpkg"          : os.path.join(BOUNDARIES_DIR, "gadm41_THA.gpkg"),
        "layer"         : "ADM_ADM_1",
        "expected_count": 77,
    },
    "India": {
        "gpkg"          : os.path.join(BOUNDARIES_DIR, "gadm41_IND.gpkg"),
        "layer"         : "ADM_ADM_1",
        # 35 thay vì 36 chính thức: GADM gadm41 không có IND.14_1
        # (Jammu & Kashmir) và Ladakh — cả 2 chỉ tồn tại dưới dạng
        # vùng tranh chấp prefix Z, đã bị filter ở load_admin1().
        # Ladakh tách ra từ J&K năm 2019, GADM version này chưa cập nhật.
        "expected_count": 35,
    },
}

# ── Core ──────────────────────────────────────────────────────────────────────

def load_admin1(gpkg_path, layer):
    """
    Đọc layer admin level-1 từ GeoPackage.
    Reproject sang WGS84 (EPSG:4326) nếu cần —
    đảm bảo centroid và geometry luôn ở lon/lat chuẩn.

    Filter bỏ các vùng tranh chấp biên giới mà GADM đánh prefix "Z"
    vào GID_1 (ví dụ Z07.3_1, Z01.14_1) — các vùng này không có dân
    số thực tế và không có dữ liệu WorldPop/RWI phủ.
    """
    gdf = gpd.read_file(gpkg_path, layer=layer)
    if gdf.crs is None or gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(epsg=4326)
    disputed = gdf["GID_1"].str.startswith("Z")
    if disputed.any():
        print(f"  Filter bỏ {disputed.sum()} vùng tranh chấp (GID_1 prefix 'Z'): "
              f"{gdf.loc[disputed, 'GID_1'].tolist()}")
        gdf = gdf[~disputed].reset_index(drop=True)
    return gdf


def verify(gdf, country, expected_count):
    """Kiểm tra số lượng và geometry hợp lệ, in kết quả."""
    actual = len(gdf)
    null_geom = gdf.geometry.isnull().sum()
    status = "OK" if actual == expected_count and null_geom == 0 else "WARN"
    icon = "✅" if status == "OK" else "⚠️"
    print(f"  {icon} {country}: {actual} admin units "
          f"(expected {expected_count}), {null_geom} null geometry")
    if actual != expected_count:
        print(f"      → Số lượng lệch {actual - expected_count:+d} "
              f"so với expected — kiểm tra lại layer name")
    if null_geom > 0:
        print(f"      → {null_geom} polygon bị null geometry — "
              f"sẽ gây lỗi ở spatial join bước sau")


def build_records(gdf, country):
    """
    Chuyển GeoDataFrame thành list of dict theo schema output.
    Centroid tính trên geometry gốc (polygon), không phải điểm đã project.
    """
    records = []
    for _, row in gdf.iterrows():
        centroid = row.geometry.centroid
        records.append({
            "country"     : country,
            "admin_code"  : row["GID_1"],
            "admin_name"  : row["NAME_1"],
            "centroid_lon": round(centroid.x, 6),
            "centroid_lat": round(centroid.y, 6),
            "geometry"    : row.geometry.__geo_interface__,
        })
    return records

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 58)
    print(" Bước 0 — Province Boundary")
    print("=" * 58)

    all_records = []

    for country, cfg in COUNTRIES.items():
        print(f"\n[{country}]")
        print(f"  Đọc: {os.path.basename(cfg['gpkg'])} / {cfg['layer']}")

        gdf = load_admin1(cfg["gpkg"], cfg["layer"])
        verify(gdf, country, cfg["expected_count"])

        records = build_records(gdf, country)
        all_records.extend(records)
        print(f"  → {len(records)} records built")

    # Ghi output
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 58}")
    print(f" Output: {os.path.abspath(OUTPUT_JSON)}")
    print(f" Tổng  : {len(all_records)} records "
          f"({sum(1 for r in all_records if r['country'] == 'Thailand')} THA + "
          f"{sum(1 for r in all_records if r['country'] == 'India')} IND)")
    print(f"{'=' * 58}")
    print("\nVerify thủ công:")
    print("  - Mở boundaries.json, kiểm tra 1 record Thailand và 1 India")
    print("  - centroid_lon/lat trông hợp lý (THA ~100°E/15°N, IND ~79°E/22°N)")
    print("  - geometry có dạng Polygon hoặc MultiPolygon")
    print("  - Bước tiếp: step1_rwi.py (cần boundaries.json này)")


if __name__ == "__main__":
    main()