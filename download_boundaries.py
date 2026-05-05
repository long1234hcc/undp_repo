"""
download_boundaries.py
────────────────────────────────────────────────────────────────
Download GeoJSON ranh giới hành chính cấp tỉnh (level 1)
cho toàn bộ Đông Nam Á từ GADM 4.1

Yêu cầu:
  pip install requests

Output:
  ./boundaries/  ← thư mục chứa file GeoJSON từng nước
  ./boundaries/sea_level1_merged.geojson  ← file gộp toàn ĐNA
"""

import os
import json
import time
import requests
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── 11 quốc gia Đông Nam Á ────────────────────────────────────
# country_code: mã ISO 3166-1 alpha-3 dùng trong GADM URL
SEA_COUNTRIES = [
    {"name": "Vietnam",     "code": "VNM"},
    {"name": "Thailand",    "code": "THA"},
    {"name": "Myanmar",     "code": "MMR"},
    {"name": "Cambodia",    "code": "KHM"},
    {"name": "Laos",        "code": "LAO"},
    {"name": "Malaysia",    "code": "MYS"},
    {"name": "Indonesia",   "code": "IDN"},
    {"name": "Philippines", "code": "PHL"},
    {"name": "Singapore",   "code": "SGP"},
    {"name": "Brunei",      "code": "BRN"},
    {"name": "Timor-Leste", "code": "TLS"},
]

# GADM 4.1 GeoJSON download URL pattern
# level=1 → ranh giới tỉnh/bang
GADM_URL = "https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_{code}_1.json"

OUTPUT_DIR = "./boundaries"


def download_country(country: dict) -> dict | None:
    """Download GeoJSON level 1 cho 1 quốc gia."""
    url  = GADM_URL.format(code=country["code"])
    path = os.path.join(OUTPUT_DIR, f"{country['code']}_level1.geojson")

    # Bỏ qua nếu đã download
    if os.path.exists(path):
        log.info(f"  [Skip] {country['name']} đã có → {path}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    log.info(f"  [Download] {country['name']} ({country['code']}) ...")
    try:
        resp = requests.get(url, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

        n = len(data.get("features", []))
        log.info(f"    ✓ {n} provinces/states → {path}")
        return data

    except requests.HTTPError as e:
        log.error(f"    ✗ HTTP {e.response.status_code} — {country['name']}")
        return None
    except Exception as e:
        log.error(f"    ✗ Lỗi: {e}")
        return None


def merge_geojson(all_data: list[dict]) -> dict:
    """
    Gộp tất cả GeoJSON features từ nhiều quốc gia thành 1 file.
    Giữ lại các thuộc tính quan trọng: tên tỉnh, tên nước, mã ISO.
    """
    merged_features = []

    for country_geojson in all_data:
        if not country_geojson:
            continue
        for feature in country_geojson.get("features", []):
            props = feature.get("properties", {})
            # Chuẩn hóa properties — GADM dùng tên field khác nhau
            merged_features.append({
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {
                    "province_name": props.get("NAME_1", ""),
                    "country_name":  props.get("COUNTRY", ""),
                    "country_code":  props.get("GID_0", ""),
                    "gid_1":         props.get("GID_1", ""),  # unique ID
                    # Giữ type code nếu có (e.g. "Province", "State")
                    "type":          props.get("TYPE_1", ""),
                }
            })

    return {
        "type":     "FeatureCollection",
        "features": merged_features,
    }


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    log.info("=" * 55)
    log.info("  GADM 4.1 — Download SEA Boundaries (Level 1)")
    log.info("=" * 55)

    all_data = []
    for i, country in enumerate(SEA_COUNTRIES):
        data = download_country(country)
        all_data.append(data)
        # Delay nhỏ để không hammering server
        if i < len(SEA_COUNTRIES) - 1:
            time.sleep(1)

    # Gộp thành 1 file
    log.info("\n[Merge] Gộp tất cả thành sea_level1_merged.geojson ...")
    merged = merge_geojson(all_data)
    merged_path = os.path.join(OUTPUT_DIR, "sea_level1_merged.geojson")

    with open(merged_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)

    log.info(f"[Done] Tổng {len(merged['features'])} provinces/states")
    log.info(f"       Saved → {merged_path}")
    log.info("")
    log.info("[Verify] Mẫu 3 features đầu tiên:")
    for feat in merged["features"][:3]:
        p = feat["properties"]
        log.info(f"  {p['country_code']} | {p['country_name']} | {p['province_name']} ({p['type']})")


if __name__ == "__main__":
    main()