"""
ingest_gadm.py
-----------------------
Ingest Layer: GADM 4.1 (UC Davis mirror) → Raw GeoJSON files trên disk.

Tải đồng thời cả ADM1 (province) và ADM2 (district) cho mỗi quốc gia.
ADM1 cần thiết làm layer hiển thị province độc lập (không chỉ để enrich district —
GADM ADM2 đã tự chứa sẵn GID_1 + NAME_1 trong properties của nó).

Khác biệt so với GeoBoundaries:
    GADM dùng URL TĨNH, suy ra trực tiếp từ ISO3 + level — không cần gọi
    metadata API trước như GeoBoundaries (gjDownloadURL). Vì vậy không còn
    bước fetch_download_url(); ingest_country() build URL thẳng rồi download.

URL pattern (đã verify thực tế ngày 2026-06-19, HTTP 200, GeoJSON hợp lệ):
    https://geodata.ucdavis.edu/gadm/gadm4.1/json/gadm41_{ISO3}_{LEVEL}.json

    Ví dụ: gadm41_THA_1.json, gadm41_THA_2.json, gadm41_IND_1.json, gadm41_IND_2.json

GID format xác nhận qua test thực tế:
    ADM1: GID_1 = "THA.1_1"      (NAME_1 = "AmnatCharoen")
    ADM2: GID_2 = "THA.1.1_1"    (NAME_2 = "Chanuman", và tự chứa GID_1 + NAME_1 của tỉnh cha)

License: GADM data chỉ miễn phí cho mục đích học thuật / phi thương mại.
         Không được redistribute hoặc dùng thương mại nếu chưa xin phép.
         (Đã xác nhận phù hợp với mục đích HeatWatch — UNDP / phi thương mại.)

Version : 1.0.0
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GADM_VERSION = "4.1"
GADM_BASE_URL = f"https://geodata.ucdavis.edu/gadm/gadm{GADM_VERSION}/json"

# Các ADM level cần tải — 1 = province, 2 = district.
# GADM dùng số nguyên (không phải "ADM1"/"ADM2" như GeoBoundaries).
ADM_LEVELS: list[int] = [1, 2]

MAX_DOWNLOAD_ATTEMPTS: int = 3
DELAY_BETWEEN_ATTEMPTS: int = 5   # giây
STREAM_TIMEOUT: int = 120         # giây cho download file lớn
CHUNK_SIZE: int = 16 * 1024       # 16KB per chunk

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CountryIngestConfig:
    """
    Khai báo một quốc gia cần ingest.

    Attributes
    ----------
    iso3         : Mã ISO-3166-1 Alpha-3 (ví dụ: "THA", "IND").
    adm_levels   : Danh sách ADM levels cần tải (default: 1 + 2).
    """

    iso3: str
    adm_levels: list[int] = field(default_factory=lambda: ADM_LEVELS)

    @property
    def display_name(self) -> str:
        return self.iso3.upper()


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------

def create_session() -> requests.Session:
    """
    Tạo Session với Retry strategy:
    - Tối đa 3 lần thử lại.
    - Backoff factor 2 → thử lại sau 2s, 4s, 8s.
    - Retry khi gặp lỗi server 500/502/503/504.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# URL builder (thay thế fetch_download_url của bản GeoBoundaries)
# ---------------------------------------------------------------------------

def build_download_url(iso3: str, adm_level: int) -> str:
    """
    Build URL tải GeoJSON từ GADM — URL tĩnh, không cần gọi metadata API.

    Pattern: {GADM_BASE_URL}/gadm41_{ISO3}_{LEVEL}.json
    """
    return f"{GADM_BASE_URL}/gadm41_{iso3.upper()}_{adm_level}.json"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_geojson(
    session: requests.Session,
    url: str,
    iso3: str,
    adm_level: int,
    output_dir: str,
) -> bool:
    """
    Tải file GeoJSON về disk qua streaming — tối ưu memory cho file lớn.
    Tự dọn dẹp file nếu download thất bại giữa chừng.

    File output: {iso3.lower()}_adm{adm_level}_raw.geojson
    Ví dụ: tha_adm1_raw.geojson, tha_adm2_raw.geojson
    """
    os.makedirs(output_dir, exist_ok=True)

    file_name = f"{iso3.lower()}_adm{adm_level}_raw.geojson"
    file_path = os.path.join(output_dir, file_name)

    logger.info(f"[{iso3}/ADM{adm_level}] Bắt đầu tải → {file_path}")

    try:
        with session.get(url, stream=True, timeout=STREAM_TIMEOUT) as response:
            response.raise_for_status()

            total_bytes = 0
            with open(file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        total_bytes += len(chunk)

        size_mb = total_bytes / (1024 * 1024)
        logger.info(
            f"[+] [{iso3}/ADM{adm_level}] Hoàn tất: {file_path} ({size_mb:.1f} MB)"
        )
        return True

    except Exception as exc:
        logger.error(f"[-] [{iso3}/ADM{adm_level}] Lỗi khi tải: {exc}")

        # Dọn dẹp file dang dở để tránh làm hỏng tầng Transform
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.warning(f"[!] [{iso3}/ADM{adm_level}] Đã xóa file không hoàn chỉnh: {file_path}")
            except OSError as del_err:
                logger.error(f"[-] Không thể xóa file lỗi: {del_err}")

        return False


def download_with_retry(
    session: requests.Session,
    url: str,
    iso3: str,
    adm_level: int,
    output_dir: str,
) -> bool:
    """
    Wrapper retry cho download_geojson.
    Thử lại tối đa MAX_DOWNLOAD_ATTEMPTS lần nếu stream crash.
    """
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        if attempt > 1:
            logger.warning(
                f"[!] [{iso3}/ADM{adm_level}] Thử lại lần {attempt}/{MAX_DOWNLOAD_ATTEMPTS} "
                f"sau {DELAY_BETWEEN_ATTEMPTS}s..."
            )
            time.sleep(DELAY_BETWEEN_ATTEMPTS)

        if download_geojson(session, url, iso3, adm_level, output_dir):
            return True

    logger.error(
        f"[-] [{iso3}/ADM{adm_level}] Thất bại sau {MAX_DOWNLOAD_ATTEMPTS} lần thử."
    )
    return False


# ---------------------------------------------------------------------------
# Per-country ingest
# ---------------------------------------------------------------------------

def ingest_country(
    session: requests.Session,
    config: CountryIngestConfig,
    output_dir: str,
) -> dict[int, bool]:
    """
    Ingest tất cả ADM levels cho một quốc gia.

    Returns
    -------
    dict[adm_level, success] : Kết quả từng level.
    """
    results: dict[int, bool] = {}

    for adm_level in config.adm_levels:
        t_start = time.perf_counter()

        # GADM: URL tĩnh, build trực tiếp — không cần gọi metadata API
        download_url = build_download_url(config.iso3, adm_level)
        logger.info(f"[{config.iso3}/ADM{adm_level}] Download URL: {download_url}")

        success = download_with_retry(session, download_url, config.iso3, adm_level, output_dir)
        results[adm_level] = success

        duration = time.perf_counter() - t_start
        if success:
            logger.info(f"[+] [{config.iso3}/ADM{adm_level}] Hoàn tất trong {duration:.1f}s.")
        else:
            logger.error(f"[-] [{config.iso3}/ADM{adm_level}] Thất bại sau {duration:.1f}s.")

    return results


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def build_ingest_manifest() -> list[CountryIngestConfig]:
    """
    Danh sách quốc gia cần ingest.
    Mỗi quốc gia sẽ tải cả ADM1 và ADM2.
    """
    return [
        CountryIngestConfig(iso3="THA"),
        CountryIngestConfig(iso3="IND"),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    raw_dir = os.path.join("data", "raw")
    manifest = build_ingest_manifest()

    logger.info("=" * 65)
    logger.info(f"   GADM {GADM_VERSION} INGEST LAYER — ADM1 + ADM2")
    logger.info("=" * 65)
    logger.info(
        f"Sẽ tải {len(manifest)} quốc gia × {len(ADM_LEVELS)} levels "
        f"= {len(manifest) * len(ADM_LEVELS)} files"
    )

    session = create_session()
    t_pipeline = time.perf_counter()

    # Tổng hợp kết quả: {iso3: {adm_level: bool}}
    all_results: dict[str, dict[int, bool]] = {}

    for config in manifest:
        logger.info(f"\n{'─' * 50}")
        logger.info(f"  Đang xử lý: {config.display_name}")
        logger.info(f"{'─' * 50}")

        all_results[config.iso3] = ingest_country(session, config, raw_dir)

    # --- Summary ---
    total_duration = time.perf_counter() - t_pipeline

    logger.info(f"\n{'=' * 65}")
    logger.info("  KẾT QUẢ INGEST")
    logger.info(f"{'=' * 65}")

    total_files = 0
    success_files = 0

    for iso3, level_results in all_results.items():
        for adm_level, success in level_results.items():
            total_files += 1
            status = "[+] OK  " if success else "[-] FAIL"
            file_name = f"{iso3.lower()}_adm{adm_level}_raw.geojson"
            logger.info(f"  {status} | {iso3}/ADM{adm_level} → {file_name}")
            if success:
                success_files += 1

    logger.info(f"{'─' * 65}")
    logger.info(
        f"  Tổng: {success_files}/{total_files} files thành công "
        f"trong {total_duration:.1f}s"
    )

    if success_files < total_files:
        failed = [
            f"{iso3}/ADM{lvl}"
            for iso3, lvls in all_results.items()
            for lvl, ok in lvls.items()
            if not ok
        ]
        logger.warning(f"  Thất bại: {failed}")


if __name__ == "__main__":
    main()