"""
transform.py
-----------------------
Transform Layer: GeoJSON (raw GADM ADM2) → Chuẩn hoá schema PostGIS.

Pipeline:
  Ingest ADM2 → Validate CRS → Validate Schema → Fix Geometry
    → Map Schema (gid_1/province_name parse TRỰC TIẾP từ properties ADM2)
    → Map Schema → Serialize Geom → Enforce Schema

Khác biệt so với bản GeoBoundaries (v3.0.0):
    GADM nhúng sẵn parent reference (GID_1 + NAME_1) ngay trong properties
    của mỗi feature ADM2 — không như GeoBoundaries phải spatial-join ADM2
    với ADM1 để suy ra tỉnh cha (representative_point + sjoin).
    → Toàn bộ _enrich_with_province() / spatial join đã được loại bỏ.
    → Pipeline transform giờ chỉ cần đọc 1 file (ADM2) duy nhất.

    File ADM1 (tha_adm1_raw.geojson, ...) vẫn được Ingest layer tải về để
    dùng cho mục đích khác (province layer riêng) — KHÔNG được transform
    layer này đọc tới.

Author  : Data Engineering Team
Version : 4.0.0
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

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

TARGET_CRS_EPSG: int = 4326  # WGS-84 — chuẩn GeoJSON RFC 7946

# Giữ nguyên schema đích — phải khớp 1-1 với INSERT_COLUMNS trong insert_postgis.py
TARGET_SCHEMA_COLUMNS: list[str] = [
    "gid_2",
    "gid_1",
    "district_name",
    "province_name",
    "country_name",
    "country_code",
    "type",
    "geom",
]

# Mapping cho format GADM 4.1 ADM2 — đã verify field thật ngày 2026-06-19:
#   GID_2, GID_1, GID_0, NAME_1, NAME_2, ENGTYPE_2 đều có sẵn trong 1 file ADM2.
GADM_ADM2_MAPPING: dict[str, str] = {
    "GID_2": "gid_2",
    "GID_1": "gid_1",
    "NAME_2": "district_name",
    "NAME_1": "province_name",
    "GID_0": "country_code",
    "ENGTYPE_2": "type",
}

# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------

@dataclass
class CountrySourceConfig:
    """
    Metadata điều khiển driver mapping cho từng nguồn dữ liệu quốc gia.

    Attributes
    ----------
    country_name   : Tên quốc gia (dùng làm giá trị cột tĩnh).
    file_name      : Tên file GeoJSON ADM2 (district) nằm trong raw_dir.
    schema_mapping : Dict ánh xạ tên cột nguồn ADM2 → tên cột đích.
                     Phải map ra đủ: gid_2, gid_1, district_name,
                     province_name, country_code, type.
    sample_size    : Số dòng lấy mẫu khi chạy test mode (None = toàn bộ).
    random_seed    : Seed cho random sample — đảm bảo reproducibility.
    """

    country_name: str
    file_name: str
    schema_mapping: dict[str, str]
    sample_size: Optional[int] = None
    random_seed: int = 42

    @property
    def required_source_columns(self) -> set[str]:
        """Tập hợp tên cột nguồn bắt buộc phải có trong file GeoJSON ADM2."""
        return set(self.schema_mapping.keys())


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _is_valid_geometry(geom) -> bool:
    """
    Kiểm tra an toàn xem một object có phải Shapely geometry hợp lệ không.
    Tránh dùng pd.isna() trực tiếp trên geometry vì sẽ raise ValueError.
    """
    if geom is None:
        return False
    if not isinstance(geom, BaseGeometry):
        return False
    if geom.is_empty:
        return False
    return True


def _downgrade_single_multipolygon(geom: BaseGeometry) -> BaseGeometry:
    """
    Hạ cấp MultiPolygon chỉ chứa 1 vùng thành Polygon đơn.
    Giúp tối ưu lưu trữ và tránh overhead khi PostGIS target column là geometry(Polygon).

    Note: Chỉ thực hiện khi target schema yêu cầu Polygon.
          Nếu column type là geometry(Geometry) thì bỏ bước này.
    """
    if geom.geom_type == "MultiPolygon" and len(geom.geoms) == 1:
        return geom.geoms[0]
    return geom


def serialize_geometry_to_geojson_string(geom) -> Optional[str]:
    """
    Chuyển đổi Shapely geometry → chuỗi GeoJSON String.

    Quy trình:
      1. Kiểm tra hợp lệ (None / empty / non-geometry).
      2. Downgrade MultiPolygon đơn về Polygon.
      3. Serialize sang JSON string.

    Returns
    -------
    str  : GeoJSON string hợp lệ.
    None : Nếu geometry không hợp lệ hoặc serialize thất bại.
    """
    if not _is_valid_geometry(geom):
        return None

    try:
        geom = _downgrade_single_multipolygon(geom)
        geojson_dict = mapping(geom)
        return json.dumps(geojson_dict, ensure_ascii=False)

    except Exception as exc:
        logger.error(f"[-] Serialize geometry thất bại: {exc}")
        return None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_source_columns(
    gdf: gpd.GeoDataFrame,
    required_columns: set[str],
    label: str,
) -> None:
    """
    Kiểm tra file nguồn có đủ các cột bắt buộc.
    Raise ValueError rõ ràng thay vì để KeyError âm thầm sau này.
    """
    missing = required_columns - set(gdf.columns)
    if missing:
        raise ValueError(
            f"[{label}] Schema mismatch — "
            f"cột không tìm thấy trong file nguồn: {sorted(missing)}. "
            f"Cột hiện có: {sorted(gdf.columns.tolist())}"
        )


def _validate_and_reproject_crs(gdf: gpd.GeoDataFrame, label: str) -> gpd.GeoDataFrame:
    """
    Kiểm tra CRS và reproject về WGS-84 (EPSG:4326) nếu cần.
    GeoJSON RFC 7946 yêu cầu WGS-84; lưu sai CRS vào PostGIS gây lỗi tọa độ.
    """
    if gdf.crs is None:
        logger.warning(
            f"[{label}] File nguồn không có CRS. "
            f"Giả định EPSG:{TARGET_CRS_EPSG} — kiểm tra lại nếu tọa độ bị lệch."
        )
        gdf = gdf.set_crs(epsg=TARGET_CRS_EPSG)
    elif gdf.crs.to_epsg() != TARGET_CRS_EPSG:
        logger.warning(
            f"[{label}] CRS nguồn là {gdf.crs.to_epsg()} "
            f"— đang reproject về EPSG:{TARGET_CRS_EPSG}."
        )
        gdf = gdf.to_crs(epsg=TARGET_CRS_EPSG)
    else:
        logger.info(f"[{label}] CRS hợp lệ: EPSG:{TARGET_CRS_EPSG}.")

    return gdf


# ---------------------------------------------------------------------------
# Fix geometry
# ---------------------------------------------------------------------------

def _fix_geometry(gdf: gpd.GeoDataFrame, label: str) -> gpd.GeoDataFrame:
    """
    Sửa self-intersection bằng buffer(0) và loại bỏ geometry trở thành empty.

    Cảnh báo: buffer(0) có thể làm mất geometry degenerate (diện tích = 0).
    Log rõ số lượng bị loại để downstream có thể audit.
    """
    n_before = len(gdf)

    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].buffer(0)

    empty_mask = gdf["geometry"].is_empty | gdf["geometry"].isna()
    n_empty = empty_mask.sum()

    if n_empty > 0:
        logger.warning(
            f"[{label}] {n_empty}/{n_before} geometry "
            f"trở thành empty sau buffer(0) — đã loại bỏ khỏi output."
        )
        gdf = gdf[~empty_mask].copy()

    logger.info(f"[{label}] Fix geometry: {n_before} → {len(gdf)} dòng hợp lệ.")
    return gdf


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _load_and_clean_adm2(
    input_path: str,
    required_source_columns: set[str],
    label: str,
    sample_size: Optional[int] = None,
    random_seed: int = 42,
) -> Optional[gpd.GeoDataFrame]:
    """
    Đọc file GeoJSON ADM2, validate CRS + schema, fix geometry.
    Không rename cột, không serialize — trả về GeoDataFrame "sạch" với
    geometry vẫn là Shapely object, sẵn sàng cho bước map schema tiếp theo.

    Returns
    -------
    GeoDataFrame : Đã validate CRS, đủ cột bắt buộc, geometry đã fix.
    None         : Nếu file không tồn tại hoặc lỗi không thể phục hồi.
    """
    if not os.path.exists(input_path):
        logger.error(f"[-] [{label}] File không tồn tại: {input_path}")
        return None

    try:
        logger.info(f"[{label}] Đang đọc file: {input_path}")
        gdf: gpd.GeoDataFrame = gpd.read_file(input_path)
        logger.info(f"[{label}] Ingest thành công: {len(gdf):,} dòng, {len(gdf.columns)} cột.")

        gdf = _validate_and_reproject_crs(gdf, label)
        _validate_source_columns(gdf, required_source_columns, label)

        if sample_size is not None:
            n_sample = min(sample_size, len(gdf))
            gdf = gdf.sample(n=n_sample, random_state=random_seed).copy()
            logger.info(
                f"[{label}] TEST MODE — sample {n_sample} dòng (random_seed={random_seed})."
            )

        gdf = _fix_geometry(gdf, label)

        if gdf.empty:
            logger.error(f"[-] [{label}] Không còn dòng nào sau khi fix geometry.")
            return None

        return gdf

    except ValueError as exc:
        logger.error(f"[-] [{label}] Lỗi validate: {exc}")
        return None
    except Exception as exc:
        logger.exception(f"[-] [{label}] Lỗi không mong đợi khi load: {exc}")
        return None


# ---------------------------------------------------------------------------
# Core transform function
# ---------------------------------------------------------------------------

def transform_geojson(
    input_path: str,
    config: CountrySourceConfig,
) -> Optional[pd.DataFrame]:
    """
    Pipeline transform chính: GeoJSON ADM2 → DataFrame chuẩn schema PostGIS.

    Steps
    -----
    1. Load + clean ADM2 (ingest, validate CRS, validate schema, sample, fix geometry).
    2. Rename columns theo schema_mapping (gid_1/province_name parse trực tiếp
       từ GID_1/NAME_1 có sẵn trong properties ADM2 — không spatial join).
    3. Serialize geometry → GeoJSON String.
    4. Thêm cột tĩnh (country_name).
    5. Enforce target schema → trả về DataFrame.

    Parameters
    ----------
    input_path : Đường dẫn tới file GeoJSON ADM2.
    config     : CountrySourceConfig chứa metadata và mapping.

    Returns
    -------
    pd.DataFrame : DataFrame chuẩn schema, sẵn sàng load vào PostGIS.
    None         : Nếu pipeline gặp lỗi không thể phục hồi.
    """
    label = config.country_name

    try:
        # 1. Load + clean ADM2
        gdf = _load_and_clean_adm2(
            input_path=input_path,
            required_source_columns=config.required_source_columns,
            label=label,
            sample_size=config.sample_size,
            random_seed=config.random_seed,
        )
        if gdf is None:
            return None

        # 2. Rename columns theo schema_mapping — gid_1/province_name lấy
        #    trực tiếp từ GID_1/NAME_1 vốn đã có sẵn trong file ADM2 (GADM).
        gdf = gdf.rename(columns=config.schema_mapping)

        # 3. Serialize geometry → GeoJSON String
        logger.info(f"[{label}] Serializing geometry...")
        gdf["geom"] = gdf["geometry"].apply(serialize_geometry_to_geojson_string)

        n_null_geom = gdf["geom"].isna().sum()
        if n_null_geom > 0:
            logger.warning(
                f"[{label}] {n_null_geom} dòng có geom=None sau serialize — "
                f"kiểm tra geometry nguồn."
            )

        # 4. Thêm cột tĩnh
        gdf["country_name"] = config.country_name

        # Cảnh báo nếu gid_1/province_name rỗng — khác GeoBoundaries, GADM lẽ ra
        # phải luôn có giá trị (không có khái niệm "unmatched" như spatial join).
        n_null_gid1 = gdf["gid_1"].isna().sum()
        if n_null_gid1 > 0:
            logger.warning(
                f"[!] [{label}] {n_null_gid1}/{len(gdf)} dòng có gid_1=NULL — "
                f"bất thường với nguồn GADM, kiểm tra lại file nguồn."
            )

        # 5. Enforce target schema — loại bỏ cột thô (geometry, v.v.)
        missing_target_cols = set(TARGET_SCHEMA_COLUMNS) - set(gdf.columns)
        if missing_target_cols:
            raise ValueError(
                f"[{label}] DataFrame thiếu cột target sau transform: "
                f"{sorted(missing_target_cols)}"
            )

        df_result = pd.DataFrame(gdf)[TARGET_SCHEMA_COLUMNS]

        logger.info(
            f"[+] [{label}] Transform hoàn tất: "
            f"{len(df_result):,} dòng × {len(df_result.columns)} cột."
        )
        return df_result

    except ValueError as exc:
        logger.error(f"[-] [{label}] Lỗi validate: {exc}")
        return None
    except Exception as exc:
        logger.exception(f"[-] [{label}] Lỗi không mong đợi trong pipeline: {exc}")
        return None


# ---------------------------------------------------------------------------
# Inspection / reporting helper
# ---------------------------------------------------------------------------

def print_transform_report(df: pd.DataFrame, country_name: str) -> None:
    """
    In báo cáo kiểm tra chất lượng output sau transform.
    Được dùng trong test mode; không gọi trong production pipeline.
    """
    separator = "=" * 80
    print(f"\n{separator}")
    print(f"  KHẢO SÁT CHUẨN HOÁ: {country_name.upper()}")
    print(separator)

    # Schema check
    print(f"\n[Schema] Cột đầu ra ({len(df.columns)} cột):")
    print(f"  {list(df.columns)}")

    # Row/null stats
    n_null_gid1 = df["gid_1"].isna().sum()
    n_null_province = df["province_name"].isna().sum()
    print(
        f"\n[Stats] Dòng: {len(df):,} | Null geom: {df['geom'].isna().sum()} | "
        f"Null gid_1: {n_null_gid1} | Null province_name: {n_null_province}"
    )

    # Attribute preview (không in cột geom)
    non_geom_cols = [c for c in df.columns if c != "geom"]
    print("\n[Preview] Thuộc tính hành chính:")
    print(df[non_geom_cols].head(20).to_string(index=True))
    if len(df) > 20:
        print(f"  ... và {len(df) - 20:,} dòng khác.")

    # Geom inspection
    print("\n[Geom] Kiểm tra cột 'geom':")
    first_valid = df["geom"].dropna()
    if first_valid.empty:
        print("  [!] Tất cả giá trị geom đều là None — serialize thất bại toàn bộ.")
    else:
        sample_val: str = first_valid.iloc[0]
        try:
            parsed = json.loads(sample_val)
            print(f"  Kiểu dữ liệu Python : {type(sample_val).__name__}")
            print(f"  Độ dài chuỗi        : {len(sample_val):,} ký tự")
            print(f"  GeoJSON type        : {parsed.get('type', 'unknown')}")
            print(f"  Preview (150 ký tự) : {sample_val[:150]}...")
        except json.JSONDecodeError:
            print(f"  [!] Giá trị geom không parse được thành JSON hợp lệ.")

    print(f"\n{'-' * 80}\n")


# ---------------------------------------------------------------------------
# Source configs
# ---------------------------------------------------------------------------

def build_sources_config() -> list[CountrySourceConfig]:
    """
    Khai báo toàn bộ nguồn dữ liệu và mapping schema.
    Trong production: load từ YAML/JSON config file thay vì hardcode tại đây.
    """
    return [
        CountrySourceConfig(
            country_name="Thailand",
            file_name="tha_adm2_raw.geojson",
            schema_mapping=GADM_ADM2_MAPPING,
            sample_size=None,  # None → xử lý toàn bộ
        ),
        CountrySourceConfig(
            country_name="India",
            file_name="ind_adm2_raw.geojson",
            schema_mapping=GADM_ADM2_MAPPING,
            sample_size=None,
        ),
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    raw_dir = os.path.join("data", "raw")
    processed_dir = os.path.join("data", "processed")
    os.makedirs(processed_dir, exist_ok=True)

    sources = build_sources_config()

    logger.info("=" * 60)
    logger.info("  TRANSFORM LAYER — GADM ADM2 → PostGIS Schema")
    logger.info("=" * 60)

    results: dict[str, Optional[pd.DataFrame]] = {}

    for config in sources:
        input_path = os.path.join(raw_dir, config.file_name)
        df = transform_geojson(input_path, config)
        results[config.country_name] = df

        if df is not None:
            print_transform_report(df, config.country_name)

            # Ghi output ra disk để insert_postgis.py đọc được
            output_path = os.path.join(
                processed_dir,
                config.file_name.replace("_raw.geojson", "_processed.json"),
            )
            df.to_json(output_path, orient="records", force_ascii=False)
            logger.info(f"[+] [{config.country_name}] Đã ghi: {output_path}")
        else:
            logger.error(f"[-] Transform thất bại cho: {config.country_name}")

        print("-" * 60)

    # Summary
    success = [k for k, v in results.items() if v is not None]
    failed = [k for k, v in results.items() if v is None]
    logger.info(f"[=] Tổng kết: {len(success)} thành công, {len(failed)} thất bại.")
    if failed:
        logger.warning(f"[!] Các quốc gia thất bại: {failed}")


if __name__ == "__main__":
    main()