"""
smoke_test.py
-------------
Kiểm tra sơ bộ tính toàn vẹn và cấu trúc của các file GeoJSON thô sau ingest.
Kiểm tra cả ADM1 (province) và ADM2 (district) cho mỗi quốc gia.
"""

from __future__ import annotations

import json
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Fields bắt buộc phải có trong properties của GeoBoundaries
REQUIRED_PROPERTIES = {"shapeName", "shapeID", "shapeGroup", "shapeType"}


def check_geojson_file(file_path: str, iso3: str, adm_level: str) -> bool:
    """
    Kiểm tra tính toàn vẹn và cấu trúc của một file GeoJSON thô.

    Returns
    -------
    True  : File hợp lệ.
    False : File lỗi hoặc không tồn tại.
    """
    label = f"{iso3}/{adm_level}"

    # --- File tồn tại ---
    if not os.path.exists(file_path):
        logger.error(f"[-] [{label}] File không tồn tại: {file_path}")
        return False

    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
    logger.info(f"[{label}] Dung lượng: {file_size_mb:.2f} MB")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # --- Cấu trúc FeatureCollection ---
        if data.get("type") != "FeatureCollection":
            logger.error(f"[-] [{label}] Không phải FeatureCollection — type: {data.get('type')}")
            return False

        features = data.get("features", [])
        if not features:
            logger.error(f"[-] [{label}] Không có feature nào trong file.")
            return False

        logger.info(f"[+] [{label}] JSON hợp lệ — {len(features):,} features.")

        # --- Kiểm tra properties của feature đầu tiên ---
        sample = features[0]
        props = sample.get("properties", {})
        geom = sample.get("geometry", {})

        missing_fields = REQUIRED_PROPERTIES - set(props.keys())
        if missing_fields:
            logger.warning(f"[!] [{label}] Thiếu fields: {sorted(missing_fields)}")
        else:
            logger.info(f"[+] [{label}] Đủ required fields: {sorted(REQUIRED_PROPERTIES)}")

        # --- Sample properties ---
        logger.info(f"[*] [{label}] Properties sample:")
        print(json.dumps(props, indent=2, ensure_ascii=False))

        # --- Geometry type ---
        geom_type = geom.get("type", "Unknown")
        logger.info(f"[*] [{label}] Geometry type: {geom_type}")

        # --- CRS ---
        crs = data.get("crs", {}).get("properties", {}).get("name", "Không có CRS")
        logger.info(f"[*] [{label}] CRS: {crs}")

        return True

    except json.JSONDecodeError as exc:
        logger.error(f"[-] [{label}] File GeoJSON bị lỗi cấu trúc: {exc}")
        return False
    except Exception as exc:
        logger.exception(f"[-] [{label}] Lỗi không xác định: {exc}")
        return False


def main() -> None:
    raw_dir = os.path.join("data", "raw")

    # Danh sách file cần kiểm tra — ADM1 + ADM2 cho mỗi quốc gia
    checks = [
        ("THA", "ADM1"),
        ("THA", "ADM2"),
        ("IND", "ADM1"),
        ("IND", "ADM2"),
    ]

    logger.info("=" * 60)
    logger.info("  SMOKE TEST — Raw GeoJSON Integrity Check")
    logger.info("=" * 60)

    results: dict[str, bool] = {}

    for iso3, adm_level in checks:
        file_name = f"{iso3.lower()}_{adm_level.lower()}_raw.geojson"
        file_path = os.path.join(raw_dir, file_name)

        print(f"\n{'─' * 60}")
        ok = check_geojson_file(file_path, iso3, adm_level)
        results[f"{iso3}/{adm_level}"] = ok

    # --- Summary ---
    print(f"\n{'=' * 60}")
    logger.info("  KẾT QUẢ SMOKE TEST")
    print(f"{'=' * 60}")

    for label, ok in results.items():
        status = "[+] OK  " if ok else "[-] FAIL"
        logger.info(f"  {status} | {label}")

    passed = sum(results.values())
    total = len(results)
    logger.info(f"{'─' * 60}")
    logger.info(f"  Tổng: {passed}/{total} files hợp lệ.")

    if passed < total:
        logger.warning("  Một số file lỗi — chạy lại ingest_geoboundaries.py trước khi transform.")


if __name__ == "__main__":
    main()