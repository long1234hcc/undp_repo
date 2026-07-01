"""
Test script: kiểm tra Open-Meteo có trả về đủ field để tính WBGT không
=========================================================================
Test với toạ độ Bangkok, Thái Lan (13.7563, 100.5018) - khu vực dự án UNDP.

Các field cần cho WBGT = 0.7*Tnwb + 0.2*Tg + 0.1*Tdb:
  - temperature_2m              -> Tdb (dry-bulb)
  - wet_bulb_temperature_2m     -> Tnwb (xấp xỉ, công thức Stull)
  - wind_speed_10m              -> input để ước lượng Tg
  - shortwave_radiation         -> input để ước lượng Tg
  - direct_radiation            -> input bổ sung (tùy chọn, chính xác hơn)
  - cloud_cover                 -> input bổ sung cho Liljegren (tùy chọn)
"""

import requests
import json

# Bangkok - đại diện khu vực dự án UNDP Thái Lan
LATITUDE = 13.7563
LONGITUDE = 100.5018

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# Các field cần test - đi từng nhóm để dễ debug nếu có field nào không hợp lệ
HOURLY_PARAMS = [
    "temperature_2m",
    "relative_humidity_2m",
    "wet_bulb_temperature_2m",
    "wind_speed_10m",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "cloud_cover",
]


def test_openmeteo_fields():
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "hourly": ",".join(HOURLY_PARAMS),
        "forecast_days": 2,
        "timezone": "Asia/Bangkok",
    }

    print(f"Đang gọi Open-Meteo API cho Bangkok ({LATITUDE}, {LONGITUDE})...")
    print(f"URL params: {params}\n")

    response = requests.get(BASE_URL, params=params, timeout=30)

    if response.status_code != 200:
        print(f"LỖI: HTTP {response.status_code}")
        print(response.text)
        return None

    data = response.json()
    return data


def print_results(data):
    if data is None:
        return

    print("=" * 70)
    print("METADATA")
    print("=" * 70)
    print(f"Latitude:  {data.get('latitude')}")
    print(f"Longitude: {data.get('longitude')}")
    print(f"Elevation: {data.get('elevation')} m")
    print(f"Timezone:  {data.get('timezone')}")

    hourly = data.get("hourly", {})
    hourly_units = data.get("hourly_units", {})
    times = hourly.get("time", [])

    print("\n" + "=" * 70)
    print("KIỂM TRA TỪNG FIELD - CÓ TRẢ VỀ DỮ LIỆU KHÔNG?")
    print("=" * 70)

    for field in HOURLY_PARAMS:
        values = hourly.get(field)
        unit = hourly_units.get(field, "?")
        if values is None:
            print(f"❌ {field:30s} -> KHÔNG có trong response")
        else:
            non_null_count = sum(1 for v in values if v is not None)
            print(
                f"✅ {field:30s} -> CÓ ({non_null_count}/{len(values)} giá trị, đơn vị: {unit})"
            )

    # In ra 12 giờ đầu tiên (1 buổi sáng -> trưa) để xem số liệu thật
    print("\n" + "=" * 70)
    print("DỮ LIỆU THỰC TẾ - 12 GIỜ ĐẦU TIÊN")
    print("=" * 70)

    header = f"{'Time':17s}"
    for field in HOURLY_PARAMS:
        short_name = field.replace("_2m", "").replace("_10m", "")[:12]
        header += f"{short_name:>14s}"
    print(header)

    for i in range(min(12, len(times))):
        row = f"{times[i]:17s}"
        for field in HOURLY_PARAMS:
            values = hourly.get(field, [])
            val = values[i] if i < len(values) else None
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else "N/A"
            row += f"{val_str:>14s}"
        print(row)

    # Lưu full JSON ra file để inspect nếu cần
    output_path = "/home/claude/openmeteo_raw_response.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"\nĐã lưu full JSON response tại: {output_path}")


if __name__ == "__main__":
    data = test_openmeteo_fields()
    print_results(data)