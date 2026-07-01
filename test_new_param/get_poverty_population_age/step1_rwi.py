"""
step1_rwi.py
============
Bước 1: Tải RWI (Relative Wealth Index) từ HDX, tính rwi_mean
per province cho Thailand và India.

Nguồn: Meta / UC Berkeley via HDX (CC BY-NC 4.0)
Resolution: ~2.4km per cell

Schema CSV nguồn (đã verify từ docs và ví dụ Jordan trên geo4.dev):
    latitude   float  vĩ độ tâm ô
    longitude  float  kinh độ tâm ô
    rwi        float  chỉ số giàu nghèo tương đối (âm = nghèo hơn tb)
    error      float  uncertainty của mô hình
    quadkey    str    mã tile Bing Maps zoom-14 (có trong bản "with-quadkeys")

Cách xử lý:
    - India   : CSV trực tiếp (~50MB), download → filter bbox India
                (file chứa cả Pakistan — cần filter)
    - Thailand: ZIP ~35MB chứa CSV global 93 nước → giải nén trong
                memory → filter bbox Thailand

Output:
    rwi_by_province.json — rwi_mean per province (112 records)

Verify sau khi chạy:
    - 112 provinces đều có rwi_mean (không null)
    - rwi_mean nằm trong khoảng [-3, 3]
    - Thailand: ~50,000–80,000 điểm RWI sau filter
    - India   : ~200,000–400,000 điểm RWI sau filter

License:
    CC BY-NC 4.0 — chỉ phi thương mại. Confirm với khách hàng trước
    khi dùng trong sản phẩm thương mại/vận hành.
"""

import io
import json
import os
import zipfile

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import shape

# ── Config ────────────────────────────────────────────────────────────────────

BOUNDARIES_JSON = "boundaries.json"
OUTPUT_JSON     = "rwi_by_province.json"
CACHE_DIR       = "cache"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# URL đã verify từ api.py (feasibility check trước đó)
RWI_URLS = {
    "India": (
        "https://data.humdata.org/dataset/76f2a2ea-ba50-40f5-b79c-db95d668b843"
        "/resource/977923ab-c65a-4203-b216-e4b7483d56a5/download"
        "/ind_pak_relative_wealth_index.csv"
    ),
    "Thailand": (
        "https://data.humdata.org/dataset/76f2a2ea-ba50-40f5-b79c-db95d668b843"
        "/resource/bff723a4-6b55-4c51-8790-6176a774e13c/download"
        "/relative-wealth-index-93-low-and-middle-income-countries"
        "-with-quadkeys-april-2021.csv"
    ),
}

# Bounding box để filter từng nước
# India: cắt bỏ Pakistan (file India+Pakistan dùng chung)
# Thailand: cắt ra khỏi file global 93 nước
BBOXES = {
    "India"   : dict(lat_min=6.5,  lat_max=37.5, lon_min=68.0, lon_max=97.5),
    "Thailand": dict(lat_min=5.6,  lat_max=20.5, lon_min=97.5, lon_max=105.7),
}

# ── Download ──────────────────────────────────────────────────────────────────

os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(country):
    return os.path.join(CACHE_DIR, f"rwi_{country.lower()}_filtered.csv")


def _filter_bbox(df, bbox):
    """Giữ lại các dòng trong bounding box."""
    return df[
        (df["latitude"]  >= bbox["lat_min"]) &
        (df["latitude"]  <= bbox["lat_max"]) &
        (df["longitude"] >= bbox["lon_min"]) &
        (df["longitude"] <= bbox["lon_max"])
    ].copy()


def load_rwi(country):
    """
    Download và filter RWI cho 1 nước. Lưu cache sau lần đầu.

    India  : CSV trực tiếp → filter bbox
    Thailand: ZIP → giải nén trong memory → đọc CSV → filter bbox
    """
    cache = _cache_path(country)
    if os.path.exists(cache):
        print(f"  Dùng cache: {cache}")
        return pd.read_csv(cache)

    url = RWI_URLS[country]
    print(f"  Downloading {country} RWI...")
    r = requests.get(url, headers=HEADERS, timeout=300)
    r.raise_for_status()
    print(f"  Đã tải: {len(r.content) / 1e6:.1f} MB")

    if url.endswith(".csv"):
        df = pd.read_csv(io.BytesIO(r.content))
    else:
        # ZIP: tìm file CSV đầu tiên bên trong rồi đọc
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            csv_names = [n for n in z.namelist() if n.endswith(".csv")]
            if not csv_names:
                raise ValueError(f"Không tìm thấy CSV trong ZIP: {z.namelist()}")
            print(f"  File CSV trong ZIP: {csv_names[0]}")
            with z.open(csv_names[0]) as f:
                df = pd.read_csv(f)

    df = _filter_bbox(df, BBOXES[country])
    df.to_csv(cache, index=False)
    print(f"  Sau filter bbox: {len(df):,} dòng → cache lưu tại {cache}")
    return df

# ── Load boundaries ───────────────────────────────────────────────────────────

def load_boundaries(json_path, country):
    with open(json_path, encoding="utf-8") as f:
        records = [r for r in json.load(f) if r["country"] == country]
    gdf = gpd.GeoDataFrame(records)
    gdf["geometry"] = gdf["geometry"].apply(shape)
    return gdf.set_geometry("geometry").set_crs(epsg=4326)

# ── Spatial join + aggregate ──────────────────────────────────────────────────

def compute_rwi_by_province(df, boundaries_gdf):
    """
    Spatial join điểm RWI → polygon province → tính rwi_mean.
    Dùng trực tiếp cột latitude/longitude từ CSV nguồn (không rename).
    """
    df = df.dropna(subset=["latitude", "longitude", "rwi"])

    gdf_pts = gpd.GeoDataFrame(
        df[["latitude", "longitude", "rwi"]],
        geometry=gpd.points_from_xy(df["longitude"], df["latitude"]),
        crs="EPSG:4326",
    )

    joined = gpd.sjoin(
        gdf_pts,
        boundaries_gdf[["admin_code", "admin_name", "geometry"]],
        how="left",
        predicate="within",
    )

    agg = (
        joined.groupby(["admin_code", "admin_name"])["rwi"]
        .agg(rwi_mean="mean", rwi_n_points="count")
        .reset_index()
    )
    agg["rwi_mean"] = agg["rwi_mean"].round(4)

    # Merge về boundaries để giữ đủ tất cả province kể cả không có điểm RWI
    result = boundaries_gdf[
        ["admin_code", "admin_name", "centroid_lon", "centroid_lat"]
    ].merge(agg, on=["admin_code", "admin_name"], how="left")

    return result

# ── Verify ────────────────────────────────────────────────────────────────────

def verify(df, country):
    null  = df["rwi_mean"].isnull().sum()
    lo    = df["rwi_mean"].min()
    hi    = df["rwi_mean"].max()
    ok    = null == 0 and -3 <= lo and hi <= 3
    icon  = "✅" if ok else "⚠️ "
    print(f"  {icon} {country}: {len(df)} provinces | "
          f"null={null} | rwi_mean=[{lo:.3f}, {hi:.3f}]")
    if null:
        missing = df[df["rwi_mean"].isnull()]["admin_name"].tolist()
        print(f"       Province null: {missing}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 58)
    print(" Bước 1 — RWI / Poverty")
    print("=" * 58)

    all_results = []

    for country in ["India", "Thailand"]:
        print(f"\n[{country}]")
        df          = load_rwi(country)
        boundaries  = load_boundaries(BOUNDARIES_JSON, country)
        result      = compute_rwi_by_province(df, boundaries)
        result["country"] = country
        verify(result, country)
        all_results.append(result)

    combined = pd.concat(all_results, ignore_index=True)
    records  = combined.to_dict(orient="records")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 58}")
    print(f" Output: {os.path.abspath(OUTPUT_JSON)}")
    print(f" Tổng  : {len(records)} records")
    print(f"{'=' * 58}")
    print("\nVerify thủ công:")
    print("  - rwi_mean âm = nghèo hơn trung bình, dương = giàu hơn")
    print("  - Bangkok / Mumbai kỳ vọng rwi_mean cao hơn vùng nông thôn")
    print("  - Bước tiếp: step2_population.py")


if __name__ == "__main__":
    main()