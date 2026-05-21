"""
MODIS HDF → JSON  (dùng netCDF4 + pyproj, hỗ trợ Sinusoidal projection)
Chạy: python modis_to_json.py
"""
import os
import sys
import json
import argparse
import warnings
import numpy as np
import netCDF4 as nc
from pyproj import Transformer

warnings.filterwarnings("ignore")

DEFAULT_FILE    = "MOD11A1.A2025100.h27v07.061.2025105033443.hdf"
DEFAULT_OUT_DIR = "./output"
# DEFAULT_LAT_MIN = 12.5
# DEFAULT_LAT_MAX = 14.5
# DEFAULT_LON_MIN = 99.5
# DEFAULT_LON_MAX = 102.5


DEFAULT_LAT_MIN = 13.5
DEFAULT_LAT_MAX = 14.0
DEFAULT_LON_MIN = 100.3
DEFAULT_LON_MAX = 100.9

# BANDS = {
#     "LST_Day_1km":   {"col": "lst_day_celsius",  "scale": 0.02,  "offset": -273.15, "fill": 0,    "is_int": False},
#     "LST_Night_1km": {"col": "lst_night_celsius", "scale": 0.02,  "offset": -273.15, "fill": 0,    "is_int": False},
#     "Emis_31":       {"col": "emissivity_band31", "scale": 0.002, "offset": 0.49,    "fill": 0,    "is_int": False},
#     "Emis_32":       {"col": "emissivity_band32", "scale": 0.002, "offset": 0.49,    "fill": 0,    "is_int": False},
#     "QC_Day":        {"col": "qc_day",            "scale": 1,     "offset": 0,       "fill": None, "is_int": True},
#     "QC_Night":      {"col": "qc_night",          "scale": 1,     "offset": 0,       "fill": None, "is_int": True},
# }

BANDS = {
    "LST_Day_1km":   {"col": "lst_day_celsius",  "offset": -273.15, "fill": 0,    "is_int": False},
    "LST_Night_1km": {"col": "lst_night_celsius", "offset": -273.15, "fill": 0,    "is_int": False},
    "Emis_31":       {"col": "emissivity_band31", "offset": 0,       "fill": 0,    "is_int": False},
    "Emis_32":       {"col": "emissivity_band32", "offset": 0,       "fill": 0,    "is_int": False},
    "QC_Day":        {"col": "qc_day",            "offset": 0,       "fill": None, "is_int": True},
    "QC_Night":      {"col": "qc_night",          "offset": 0,       "fill": None, "is_int": True},
}

# MODIS Sinusoidal projection (chuẩn NASA)
SINU_PROJ = "+proj=sinu +R=6371007.181 +nadgrids=@null +wktext"

def build_latlon_sinusoidal(ul_x, ul_y, lr_x, lr_y, nrow, ncol):
    """
    Tạo lưới lat/lon từ MODIS Sinusoidal projection.
    ul = UpperLeftPointMtrs, lr = LowerRightMtrs (đơn vị: meters)
    """
    transformer = Transformer.from_crs(SINU_PROJ, "EPSG:4326", always_xy=True)

    # Pixel centers trong không gian Sinusoidal
    xs = np.linspace(ul_x, lr_x, ncol, endpoint=False) + (lr_x - ul_x) / (2 * ncol)
    ys = np.linspace(ul_y, lr_y, nrow, endpoint=False) + (lr_y - ul_y) / (2 * nrow)

    x_grid, y_grid = np.meshgrid(xs, ys)

    # Convert sang lon/lat
    lon_grid, lat_grid = transformer.transform(x_grid.ravel(), y_grid.ravel())
    return lat_grid.reshape(nrow, ncol), lon_grid.reshape(nrow, ncol)

def parse_ul_lr(f):
    """Đọc UpperLeftPointMtrs và LowerRightMtrs từ StructMetadata."""
    struct_meta = f.getncattr("StructMetadata.0")
    import re
    ul = re.search(r"UpperLeftPointMtrs=\((.+?)\)", struct_meta)
    lr = re.search(r"LowerRightMtrs=\((.+?)\)",    struct_meta)
    ul_x, ul_y = (float(v) for v in ul.group(1).split(","))
    lr_x, lr_y = (float(v) for v in lr.group(1).split(","))
    return ul_x, ul_y, lr_x, lr_y

# def read_band(var, scale, offset, fill):
#     data = var[:].data.astype(np.float32)
#     if fill is not None:
#         data[data == fill] = np.nan
#     return np.where(np.isnan(data), np.nan, data * scale + offset)

def read_band(var, offset, fill):
    # netCDF4 tự apply scale_factor, dùng .data để lấy giá trị đã scale
    data = var[:].data.astype(np.float32)

    # Mask fill value (0 = no data)
    if fill is not None:
        data[data == fill] = np.nan

    # KHÔNG dùng valid_range vì nó là raw uint16, không phải scaled
    return np.where(np.isnan(data), np.nan, data + offset)


def to_json(filepath, lat_min, lat_max, lon_min, lon_max, out_dir):
    f    = nc.Dataset(filepath)
    nrow, ncol = f.variables["LST_Day_1km"].shape

    # Build lat/lon grid từ Sinusoidal projection
    ul_x, ul_y, lr_x, lr_y = parse_ul_lr(f)
    print(f"UL: ({ul_x:.0f}, {ul_y:.0f})  LR: ({lr_x:.0f}, {lr_y:.0f})")
    lat_grid, lon_grid = build_latlon_sinusoidal(ul_x, ul_y, lr_x, lr_y, nrow, ncol)
    print(f"Lat range: [{lat_grid.min():.2f}, {lat_grid.max():.2f}]")
    print(f"Lon range: [{lon_grid.min():.2f}, {lon_grid.max():.2f}]")

    # Crop bbox
    mask = (lat_grid >= lat_min) & (lat_grid <= lat_max) & (lon_grid >= lon_min) & (lon_grid <= lon_max)
    print(f"Pixels trong bbox: {mask.sum():,}")

    if mask.sum() == 0:
        print("❌ Không có pixel nào trong bbox!")
        f.close()
        return

    # Đọc bands
    # band_data = {
    #     name: read_band(f.variables[name], cfg["scale"], cfg["offset"], cfg["fill"])
    #     for name, cfg in BANDS.items()
    # }

    band_data = {
        name: read_band(f.variables[name], cfg["offset"], cfg["fill"])
        for name, cfg in BANDS.items()
    }
    f.close()

    lats = np.round(lat_grid[mask], 5)
    lons = np.round(lon_grid[mask], 5)
    extracted = {cfg["col"]: (band_data[name][mask], cfg["is_int"]) for name, cfg in BANDS.items()}

    records = [
        {
            "lat": float(lats[i]),
            "lon": float(lons[i]),
            **{
                col: (None if np.isnan(vals[i]) else (int(vals[i]) if is_int else round(float(vals[i]), 4)))
                for col, (vals, is_int) in extracted.items()
            }
        }
        for i in range(int(mask.sum()))
    ]

    output = {
        "metadata": {
            "source":     os.path.basename(filepath),
            "bbox":       {"lat_min": lat_min, "lat_max": lat_max, "lon_min": lon_min, "lon_max": lon_max},
            "total_rows": len(records),
        },
        "data": records,
    }

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, os.path.splitext(os.path.basename(filepath))[0] + ".json")
    with open(out_path, "w", encoding="utf-8") as jf:
        json.dump(output, jf, indent=2, ensure_ascii=False)

    print(f"✅ JSON saved → {out_path}  ({os.path.getsize(out_path)//1024} KB, {len(records):,} records)")

def main(sources:str):
    p = argparse.ArgumentParser()
    p.add_argument("--file",    default=sources)
    p.add_argument("--out_dir", default=DEFAULT_OUT_DIR)
    p.add_argument("--lat_min", type=float, default=DEFAULT_LAT_MIN)
    p.add_argument("--lat_max", type=float, default=DEFAULT_LAT_MAX)
    p.add_argument("--lon_min", type=float, default=DEFAULT_LON_MIN)
    p.add_argument("--lon_max", type=float, default=DEFAULT_LON_MAX)
    args = p.parse_args()

    if not os.path.exists(args.file):
        print(f"❌ File không tìm thấy: {args.file}")
        sys.exit(1)

    to_json(args.file, args.lat_min, args.lat_max, args.lon_min, args.lon_max, args.out_dir)

if __name__ == "__main__":
    lst_files = [
        "MOD11A1.A2025056.h27v07.061.2025058203847.hdf",
        "MOD11A1.A2025057.h27v07.061.2025058205357.hdf",
        "MOD11A1.A2025058.h27v07.061.2025060184104.hdf",
        "MOD11A1.A2025059.h27v07.061.2025060184742.hdf",
    ]
    for i in lst_files:
        main(i)