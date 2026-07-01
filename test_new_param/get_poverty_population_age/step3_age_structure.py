"""
step3_age_structure.py
======================
Bước 3: Tính age_0_14_pct, age_15_64_pct, age_65plus_pct per province
từ WorldPop Global2 age/sex structure (1km resolution).

QUAN TRỌNG — khác các bước trước:
    Response thực tế của endpoint age_structures/G2_CN_Age_R25A_1km chưa
    được tự verify 100% (giới hạn công cụ research). Script này chạy theo
    2 PHA:

    PHA 1 (discover_structure) — BẮT BUỘC chạy trước, xem output:
        Gọi API 1 lần cho Thailand, in toàn bộ cấu trúc JSON thật ra
        màn hình + lưu vào discovery_output.json.
        KHÔNG tự đoán tên field — tự động dò các key có thể là
        gender / age group / year / file url.

    PHA 2 (main pipeline) — chỉ chạy sau khi Pha 1 xác nhận cấu trúc đúng:
        Dùng field name đã dò được ở Pha 1 để tải file .tif, zonal_stats
        theo polygon, tính % 3 nhóm tuổi.

Cách chạy:
    python step3_age_structure.py --discover      # Pha 1, chạy trước
    python step3_age_structure.py                  # Pha 2, chạy sau

API đã verify chắc chắn (từ response JSON thật của age_structures gốc):
    Alias đúng: G2_CN_Age_R25A_1km
      = "Individual countries 2015-2030 (1km resolution) R2025A v1"
    Endpoint  : https://www.worldpop.org/rest/data/age_structures/{alias}?iso3={ISO3}

Filename pattern đã verify từ tài liệu HDX (World/Turks&Caicos/Cayman/Moldova
age-sex datasets, cùng 1 mô tả lặp lại nhất quán):
    {iso}_{gender}_{agegroup}_{year}_{resolution}.tif
    gender: m | f | t (t = tổng 2 giới, nếu có)
    agegroup: 00 (0-12 thang), 01 (1-4y), 05, 10, ..., 80, 90+ (tuy version)

Output cuoi:
    age_structure_by_province.json - 112 records
"""

import argparse
import json
import os

import geopandas as gpd
import requests
from shapely.geometry import shape

try:
    from rasterstats import zonal_stats
except ImportError:
    zonal_stats = None  # chi can khi chay Pha 2

# Config

BOUNDARIES_JSON = "boundaries.json"
OUTPUT_JSON     = "age_structure_by_province.json"
CACHE_DIR       = "cache"
DISCOVERY_FILE  = "discovery_output.json"

WORLDPOP_API = "https://www.worldpop.org/rest/data/age_structures"
ALIAS        = "G2_CN_Age_R25A_1km"
TARGET_YEAR  = "2020"

COUNTRIES = {"Thailand": "THA", "India": "IND"}

AGE_GROUP_BUCKETS = {
    "0_14":    ["00", "01", "05", "10"],
    "15_64":   ["15", "20", "25", "30", "35", "40", "45", "50", "55", "60"],
    "65plus":  ["65", "70", "75", "80", "85", "90"],
}

os.makedirs(CACHE_DIR, exist_ok=True)

# PHA 1: Discovery

def discover_structure():
    url = f"{WORLDPOP_API}/{ALIAS}?iso3=THA"
    print(f"Goi API: {url}\n")

    r = requests.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    with open(DISCOVERY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Da luu toan bo response vao: {os.path.abspath(DISCOVERY_FILE)}\n")

    records = data.get("data", [])
    print(f"Tong so record tra ve: {len(records)}\n")

    if not records:
        print("CANH BAO: API tra ve rong - alias hoac iso3 co the sai, hoac")
        print("endpoint yeu cau them tham so. Mo file discovery_output.json")
        print("de xem message loi/thong bao day du (neu co).")
        return

    print("=== Mau 1 record dau tien (toan bo field) ===")
    print(json.dumps(records[0], ensure_ascii=False, indent=2))

    print("\n=== Tat ca field keys xuat hien (union toan bo record) ===")
    all_keys = set()
    for rec in records:
        all_keys.update(rec.keys())
    print(sorted(all_keys))

    print("\n=== Do field kha nghi ===")
    for key in sorted(all_keys):
        sample_vals = list({str(rec.get(key)) for rec in records[:20]})[:5]
        print(f"  {key:20s} -> mau gia tri: {sample_vals}")

    print(
        "\nBUOC TIEP: xem ky output tren (hoac mo discovery_output.json).\n"
        "Xac dinh:\n"
        "  - Field nao chua nam (vd 'popyear' hay 'year')\n"
        "  - Field nao chua gioi tinh (vd 'sex' hay 'gender')\n"
        "  - Field nao chua nhom tuoi (vd 'agegroup' hay 'age')\n"
        "  - Field nao chua URL file (thuong la 'files', dang list)\n"
        "Cap nhat FIELD_MAP trong code cho khop, roi chay lai khong co\n"
        "--discover de vao Pha 2.\n"
    )

# FIELD MAP - cap nhat sau khi xem ket qua discover_structure()
FIELD_MAP = {
    "year":     "popyear",
    "gender":   "sex",
    "agegroup": "agegroup",
    "files":    "files",
}

# PHA 2: Main pipeline

def fetch_age_records(iso3):
    url = f"{WORLDPOP_API}/{ALIAS}?iso3={iso3}"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    records = r.json().get("data", [])

    if not records:
        raise ValueError(f"API tra ve rong cho {iso3}. Chay --discover de kiem tra.")

    sample = records[0]
    missing = [v for v in FIELD_MAP.values() if v not in sample]
    if missing:
        raise KeyError(
            f"Field {missing} khong ton tai trong response that. "
            f"Field co san: {list(sample.keys())}. "
            f"Chay 'python step3_age_structure.py --discover' de xem cau truc "
            f"dung, roi sua FIELD_MAP trong code cho khop."
        )

    year_field = FIELD_MAP["year"]
    filtered = [r for r in records if str(r.get(year_field)) == TARGET_YEAR]
    if not filtered:
        available_years = sorted({str(r.get(year_field)) for r in records})
        raise ValueError(
            f"Khong co data nam {TARGET_YEAR} cho {iso3}. "
            f"Nam co san: {available_years}"
        )
    return filtered


def pick_gender_records(records):
    gender_field = FIELD_MAP["gender"]
    genders_available = {r.get(gender_field) for r in records}

    if "t" in genders_available:
        print("  Dung gender='t' (tong 2 gioi co san)")
        return [r for r in records if r.get(gender_field) == "t"], False
    else:
        print(f"  Khong co gender='t' (co: {genders_available}) "
              f"-> se cong m + f thu cong")
        return records, True


def download_tif(url, cache_name):
    cache_path = os.path.join(CACHE_DIR, cache_name)
    if os.path.exists(cache_path):
        return cache_path
    r = requests.get(url, stream=True, timeout=300)
    r.raise_for_status()
    with open(cache_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    return cache_path


def load_boundaries(json_path, country):
    with open(json_path, encoding="utf-8") as f:
        records = [r for r in json.load(f) if r["country"] == country]
    gdf = gpd.GeoDataFrame(records)
    gdf["geometry"] = gdf["geometry"].apply(shape)
    return gdf.set_geometry("geometry").set_crs(epsg=4326)


def sum_zonal_stats_for_bucket(records, agegroup_codes, need_sum_gender,
                                boundaries_gdf, iso3, bucket_name):
    agegroup_field = FIELD_MAP["agegroup"]
    files_field    = FIELD_MAP["files"]

    matching = [r for r in records if r.get(agegroup_field) in agegroup_codes]
    if not matching:
        print(f"    CANH BAO: khong tim thay record nao cho agegroup {agegroup_codes}")
        return {code: 0.0 for code in boundaries_gdf["admin_code"]}

    total_per_province = {code: 0.0 for code in boundaries_gdf["admin_code"]}

    for rec in matching:
        file_urls = rec.get(files_field, [])
        if not file_urls:
            continue
        url = file_urls[0]
        cache_name = f"age_{iso3.lower()}_{bucket_name}_{os.path.basename(url)}"
        tif_path = download_tif(url, cache_name)

        stats = zonal_stats(boundaries_gdf, tif_path, stats=["sum"], nodata=-99999)
        for i, admin_code in enumerate(boundaries_gdf["admin_code"]):
            val = stats[i]["sum"] or 0
            total_per_province[admin_code] += val

    return total_per_province


def compute_age_structure(iso3, country, boundaries_gdf):
    print(f"\n[{country} / {iso3}]")
    records = fetch_age_records(iso3)
    records, need_sum_gender = pick_gender_records(records)

    bucket_totals = {}
    for bucket_name, codes in AGE_GROUP_BUCKETS.items():
        print(f"  Dang tinh bucket {bucket_name}...")
        bucket_totals[bucket_name] = sum_zonal_stats_for_bucket(
            records, codes, need_sum_gender, boundaries_gdf, iso3, bucket_name
        )

    results = []
    for _, row in boundaries_gdf.iterrows():
        code = row["admin_code"]
        t0 = bucket_totals["0_14"][code]
        t1 = bucket_totals["15_64"][code]
        t2 = bucket_totals["65plus"][code]
        total = t0 + t1 + t2
        if total == 0:
            pct0 = pct1 = pct2 = None
        else:
            pct0 = round(t0 / total * 100, 2)
            pct1 = round(t1 / total * 100, 2)
            pct2 = round(t2 / total * 100, 2)
        results.append({
            "admin_code":     code,
            "admin_name":     row["admin_name"],
            "country":        country,
            "age_0_14_pct":   pct0,
            "age_15_64_pct":  pct1,
            "age_65plus_pct": pct2,
        })
    return results


def verify(records, country):
    null = sum(1 for r in records if r["age_0_14_pct"] is None)
    bad_sum = sum(
        1 for r in records
        if r["age_0_14_pct"] is not None and
        abs(r["age_0_14_pct"] + r["age_15_64_pct"] + r["age_65plus_pct"] - 100) > 1
    )
    icon = "OK" if null == 0 and bad_sum == 0 else "CANH BAO"
    print(f"  [{icon}] {country}: {len(records)} provinces | "
          f"null={null} | tong khac 100%: {bad_sum}")


def main_pipeline():
    print("=" * 58)
    print(" Buoc 3 - Age Structure (WorldPop Global2, 1km)")
    print("=" * 58)

    all_results = []
    for country, iso3 in COUNTRIES.items():
        boundaries = load_boundaries(BOUNDARIES_JSON, country)
        results = compute_age_structure(iso3, country, boundaries)
        verify(results, country)
        all_results.extend(results)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"\nOutput: {os.path.abspath(OUTPUT_JSON)}")
    print(f"Tong: {len(all_results)} records")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true",
                         help="Chay Pha 1: do cau truc API that")
    args = parser.parse_args()

    if args.discover:
        discover_structure()
    else:
        if zonal_stats is None:
            raise SystemExit(
                "Thieu thu vien rasterstats. Chay:\n"
                "  pip install rasterstats rasterio --break-system-packages"
            )
        main_pipeline()