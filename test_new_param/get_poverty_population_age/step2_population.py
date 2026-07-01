"""
step2_population.py
===================
Bước 2: Tính population_total per province từ WorldPop 1km ASCII XYZ.

Nguồn: WorldPop — Unconstrained individual countries 2000-2020
        UN adjusted (1km resolution)
        https://hub.worldpop.org/rest/data/pop/wpicuadj1km?iso3={ISO3}
License: Creative Commons Attribution 4.0 (CC BY 4.0) — dùng được cả
         mục đích thương mại, chỉ cần ghi attribution.

Format ASCII XYZ (đã verify từ HDX + WorldPop docs):
    File ZIP chứa 1 file .csv (hoặc .xyz) với 3 cột:
        X = longitude
        Y = latitude
        Z = population count (số người per pixel, không phải density)
    Units: number of people per 1km² pixel (UN-adjusted)

Flow giống hệt step1_rwi.py:
    1. Gọi WorldPop REST API → lấy URL file ZIP
    2. Download ZIP → giải nén trong memory → đọc CSV
    3. Spatial join điểm → polygon province
    4. Sum population per province

Output:
    population_by_province.json — 112 records

Verify:
    Thailand tổng ≈ 65–75 triệu
    India tổng    ≈ 1.3–1.45 tỉ
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
OUTPUT_JSON     = "population_by_province.json"
CACHE_DIR       = "cache"

WORLDPOP_API    = "https://www.worldpop.org/rest/data/pop/wpicuadj1km"
YEAR            = "2020"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

COUNTRIES = {
    "Thailand": "THA",
    "India"   : "IND",
}

EXPECTED_POP = {
    "Thailand": (65_000_000,  75_000_000),
    "India"   : (1_300_000_000, 1_450_000_000),
}

# ── API: lấy URL file ZIP từ WorldPop ────────────────────────────────────────

def get_zip_url(iso3, year):
    """
    Gọi WorldPop REST API alias wpicuadj1km, lấy URL file ZIP cho năm cụ thể.
    Trả về URL file ZIP chứa ASCII XYZ CSV.
    """
    r = requests.get(f"{WORLDPOP_API}?iso3={iso3}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    records = r.json().get("data", [])
    match = next((d for d in records if d.get("popyear") == year), None)
    if not match:
        raise ValueError(f"Không tìm thấy dataset năm {year} cho {iso3}")
    files = match.get("files", [])
    # Ưu tiên file ZIP (ASCII XYZ), fallback về .tif nếu không có ZIP
    zip_url = next((f for f in files if f.endswith(".zip")), None)
    if not zip_url:
        raise ValueError(f"Không tìm thấy file ZIP trong: {files}")
    print(f"  URL từ API: {zip_url}")
    return zip_url

# ── Download + đọc CSV ────────────────────────────────────────────────────────

os.makedirs(CACHE_DIR, exist_ok=True)


def _cache_path(iso3):
    return os.path.join(CACHE_DIR, f"pop_{iso3.lower()}_1km.csv")


def load_population_csv(iso3, year):
    """
    Download ZIP, giải nén trong memory, đọc file CSV/XYZ bên trong.
    Lưu cache CSV đã giải nén để re-run không tải lại.

    Columns trả về: X (lon), Y (lat), Z (population count per pixel)
    """
    cache = _cache_path(iso3)
    if os.path.exists(cache):
        print(f"  Dùng cache: {cache}")
        return pd.read_csv(cache)

    zip_url = get_zip_url(iso3, year)
    print(f"  Downloading ZIP...")
    r = requests.get(zip_url, headers=HEADERS, timeout=300)
    r.raise_for_status()
    print(f"  Đã tải: {len(r.content) / 1e6:.1f} MB")

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        # Tìm file dữ liệu bên trong ZIP (thường là .csv hoặc .xyz hoặc .txt)
        data_files = [n for n in z.namelist()
                      if not n.startswith("__") and
                      any(n.endswith(ext) for ext in (".csv", ".xyz", ".txt"))]
        if not data_files:
            raise ValueError(f"Không tìm thấy file dữ liệu trong ZIP: {z.namelist()}")
        print(f"  File trong ZIP: {data_files[0]}")
        with z.open(data_files[0]) as f:
            df = pd.read_csv(f, header=0)

    # Chuẩn hóa tên cột: WorldPop ASCII XYZ dùng X, Y, Z
    df.columns = [c.strip().upper() for c in df.columns]
    df = df.rename(columns={"X": "lon", "Y": "lat", "Z": "population"})
    df = df[df["population"] > 0].reset_index(drop=True)

    df.to_csv(cache, index=False)
    print(f"  Sau filter (pop > 0): {len(df):,} điểm → cache: {cache}")
    return df

# ── Load boundaries ───────────────────────────────────────────────────────────

def load_boundaries(json_path, country):
    with open(json_path, encoding="utf-8") as f:
        records = [r for r in json.load(f) if r["country"] == country]
    gdf = gpd.GeoDataFrame(records)
    gdf["geometry"] = gdf["geometry"].apply(shape)
    return gdf.set_geometry("geometry").set_crs(epsg=4326)

# ── Spatial join + aggregate ──────────────────────────────────────────────────

def compute_population_by_province(df, boundaries_gdf):
    """
    Spatial join điểm (lon/lat) → polygon province → sum population.
    """
    gdf_pts = gpd.GeoDataFrame(
        df[["lon", "lat", "population"]],
        geometry=gpd.points_from_xy(df["lon"], df["lat"]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(
        gdf_pts,
        boundaries_gdf[["admin_code", "admin_name", "geometry"]],
        how="left",
        predicate="within",
    )
    agg = (
        joined.groupby(["admin_code", "admin_name"])["population"]
        .sum()
        .reset_index()
        .rename(columns={"population": "population_total"})
    )
    agg["population_total"] = agg["population_total"].round().astype(int)

    result = boundaries_gdf[
        ["admin_code", "admin_name", "centroid_lon", "centroid_lat"]
    ].merge(agg, on=["admin_code", "admin_name"], how="left")
    return result

# ── Verify ────────────────────────────────────────────────────────────────────

def verify(df, country):
    null  = df["population_total"].isnull().sum()
    total = df["population_total"].sum()
    lo, hi = EXPECTED_POP[country]
    ok    = null == 0 and lo <= total <= hi
    icon  = "✅" if ok else "⚠️ "
    print(f"  {icon} {country}: {len(df)} provinces | null={null} | "
          f"total={total:,.0f} ({'OK' if lo<=total<=hi else f'ngoài range [{lo:,}–{hi:,}]'})")
    if null:
        print(f"     Province null: "
              f"{df[df['population_total'].isnull()]['admin_name'].tolist()}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 58)
    print(f" Bước 2 — Population (WorldPop 1km ASCII XYZ, {YEAR})")
    print("=" * 58)

    all_results = []

    for country, iso3 in COUNTRIES.items():
        print(f"\n[{country} / {iso3}]")
        df         = load_population_csv(iso3, YEAR)
        boundaries = load_boundaries(BOUNDARIES_JSON, country)
        result     = compute_population_by_province(df, boundaries)
        result["country"] = country
        verify(result, country)
        all_results.append(
            result[["admin_code", "admin_name", "country",
                     "centroid_lon", "centroid_lat", "population_total"]]
        )

    combined = pd.concat(all_results, ignore_index=True)
    records  = combined.to_dict(orient="records")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 58}")
    print(f" Output: {os.path.abspath(OUTPUT_JSON)}")
    print(f" Tổng  : {len(records)} records")
    print(f"{'=' * 58}")
    print("\nVerify thủ công:")
    print("  - Bangkok kỳ vọng > 5 triệu")
    print("  - Uttar Pradesh kỳ vọng > 200 triệu")
    print("  - Bước tiếp: step3_age_structure.py")


if __name__ == "__main__":
    main()