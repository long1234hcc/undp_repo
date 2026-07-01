"""
Test script: kiểm tra Open-Meteo có trả về đủ field để tính WBGT không
=========================================================================
Test 2 vùng khí hậu khác nhau để kiểm tra độ ổn định dữ liệu:
  - Bangkok, Thailand   (13.7563, 100.5018) - nóng ẩm, ven biển/đồng bằng
  - New Delhi, India    (28.6139, 77.2090)  - nóng khô, nội lục

Các field cần cho WBGT = 0.7*Tnwb + 0.2*Tg + 0.1*Tdb:
  - temperature_2m              -> Tdb (dry-bulb, dùng thẳng, không cần build)
  - wet_bulb_temperature_2m     -> chỉ để THAM KHẢO/đối chiếu (đã chốt: tự build
                                    Tnwb theo Liljegren, không dùng field này)
  - wind_speed_10m              -> input bắt buộc cho Tg và Tnwb (Liljegren)
                                    *** ĐƠN VỊ: ép về m/s qua wind_speed_unit=ms,
                                    vì mặc định Open-Meteo trả km/h, trong khi
                                    công thức Liljegren cần input m/s. Không ép
                                    đơn vị sẽ làm Tg/Tnwb sai lệch ~3.6 lần ở
                                    thành phần đối lưu mà không có lỗi crash nào
                                    báo hiệu. ***
  - shortwave_radiation         -> input bắt buộc cho Tg (tổng bức xạ)
  - direct_radiation            -> input để tính tỷ lệ fdir (bức xạ trực tiếp) cho Tg
  - diffuse_radiation           -> input để tính tỷ lệ fdir (bức xạ khuếch tán) cho Tg
  - cloud_cover                 -> input bổ sung (tùy chọn, kiểm tra chéo với radiation)
  - surface_pressure            -> input cho hệ số truyền nhiệt đối lưu (h) trong
                                    Tg/Tnwb. Đơn vị mặc định là hPa, khớp trực tiếp
                                    với công thức Liljegren, KHÔNG cần convert.

Đã verify tên field + đơn vị mặc định trực tiếp với tài liệu API chính thức
(open-meteo.com/en/docs) trước khi viết script này.
"""

import requests
import json
import os

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# 2 vùng test - khí hậu khác nhau để kiểm tra độ ổn định dữ liệu
LOCATIONS = [
    {
        "name": "Bangkok, Thailand",
        "latitude": 13.7563,
        "longitude": 100.5018,
        "timezone": "Asia/Bangkok",
        "expected_elevation_m": 2,   # Bangkok gần như ngang mực nước biển
    },
    {
        "name": "New Delhi, India",
        "latitude": 28.6139,
        "longitude": 77.2090,
        "timezone": "Asia/Kolkata",
        "expected_elevation_m": 216, # elevation thực tế của New Delhi
    },
]

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
    "surface_pressure",
]

# Khoảng giá trị hợp lý để sanity-check (không phải giới hạn vật lý tuyệt đối,
# chỉ là ngưỡng rộng để bắt lỗi rõ ràng: đơn vị sai, dữ liệu null hàng loạt, v.v.)
SANITY_RANGES = {
    "temperature_2m": (-10, 55),          # °C
    "relative_humidity_2m": (0, 100),     # %
    "wet_bulb_temperature_2m": (-10, 40), # °C, luôn <= temperature_2m
    "wind_speed_10m": (0, 50),            # m/s (đã ép unit ms) - >50 m/s là bất thường
    "shortwave_radiation": (0, 1200),     # W/m^2
    "direct_radiation": (0, 1100),        # W/m^2
    "diffuse_radiation": (0, 700),        # W/m^2
    "cloud_cover": (0, 100),              # %
    "surface_pressure": (850, 1050),      # hPa (giảm theo elevation, vẫn trong khoảng này ở đa số nơi)
}


def fetch_openmeteo(location):
    params = {
        "latitude": location["latitude"],
        "longitude": location["longitude"],
        "hourly": ",".join(HOURLY_PARAMS),
        "forecast_days": 2,
        "timezone": location["timezone"],
        "wind_speed_unit": "ms",  # *** fix bug đơn vị - bắt buộc phải có dòng này ***
    }

    print(f"Đang gọi Open-Meteo API cho {location['name']} ({location['latitude']}, {location['longitude']})...")
    print(f"URL params: {params}\n")

    response = requests.get(BASE_URL, params=params, timeout=30)

    if response.status_code != 200:
        print(f"LỖI: HTTP {response.status_code}")
        print(response.text)
        return None

    return response.json()


def check_field_availability(hourly, hourly_units):
    print("=" * 70)
    print("KIỂM TRA TỪNG FIELD - CÓ TRẢ VỀ DỮ LIỆU KHÔNG?")
    print("=" * 70)

    all_ok = True
    for field in HOURLY_PARAMS:
        values = hourly.get(field)
        unit = hourly_units.get(field, "?")
        if values is None:
            print(f"❌ {field:30s} -> KHÔNG có trong response")
            all_ok = False
        else:
            non_null_count = sum(1 for v in values if v is not None)
            status = "✅" if non_null_count == len(values) else "⚠️ "
            if non_null_count < len(values):
                all_ok = False
            print(
                f"{status} {field:30s} -> CÓ ({non_null_count}/{len(values)} giá trị, đơn vị: {unit})"
            )
    return all_ok


def check_sanity_ranges(hourly):
    print("\n" + "=" * 70)
    print("SANITY CHECK - GIÁ TRỊ CÓ NẰM TRONG KHOẢNG HỢP LÝ KHÔNG?")
    print("=" * 70)

    all_ok = True
    for field, (lo, hi) in SANITY_RANGES.items():
        values = [v for v in hourly.get(field, []) if v is not None]
        if not values:
            continue
        v_min, v_max = min(values), max(values)
        in_range = (v_min >= lo) and (v_max <= hi)
        status = "✅" if in_range else "❌"
        if not in_range:
            all_ok = False
        print(
            f"{status} {field:30s} -> min={v_min:.2f}, max={v_max:.2f} "
            f"(khoảng kỳ vọng: {lo} - {hi})"
        )

    # Check chéo: wet_bulb luôn phải <= dry-bulb tại mọi thời điểm
    tdb = hourly.get("temperature_2m", [])
    twb = hourly.get("wet_bulb_temperature_2m", [])
    if tdb and twb and len(tdb) == len(twb):
        violations = sum(
            1 for a, b in zip(tdb, twb)
            if a is not None and b is not None and b > a + 0.01  # +0.01 cho sai số làm tròn
        )
        status = "✅" if violations == 0 else "❌"
        if violations > 0:
            all_ok = False
        print(f"{status} {'wet_bulb <= dry_bulb check':30s} -> {violations} vi phạm / {len(tdb)} điểm")

    # Check chéo: direct + diffuse phải xấp xỉ shortwave (cho phép sai số ~5%)
    sw = hourly.get("shortwave_radiation", [])
    direct = hourly.get("direct_radiation", [])
    diffuse = hourly.get("diffuse_radiation", [])
    if sw and direct and diffuse and len(sw) == len(direct) == len(diffuse):
        mismatches = 0
        checked = 0
        for s, d, df in zip(sw, direct, diffuse):
            if s is None or d is None or df is None or s < 10:  # bỏ qua ban đêm/giá trị quá nhỏ
                continue
            checked += 1
            if abs((d + df) - s) > 0.05 * s:
                mismatches += 1
        status = "✅" if mismatches == 0 else "⚠️ "
        print(f"{status} {'direct+diffuse ~= shortwave check':30s} -> {mismatches} lệch / {checked} điểm kiểm tra")

    return all_ok


def print_sample_rows(hourly, times, n=12):
    print("\n" + "=" * 70)
    print(f"DỮ LIỆU THỰC TẾ - {n} GIỜ ĐẦU TIÊN")
    print("=" * 70)

    header = f"{'Time':17s}"
    for field in HOURLY_PARAMS:
        short_name = field.replace("_2m", "").replace("_10m", "")[:12]
        header += f"{short_name:>14s}"
    print(header)

    for i in range(min(n, len(times))):
        row = f"{times[i]:17s}"
        for field in HOURLY_PARAMS:
            values = hourly.get(field, [])
            val = values[i] if i < len(values) else None
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else "N/A"
            row += f"{val_str:>14s}"
        print(row)


def run_test_for_location(location):
    print("\n" + "#" * 70)
    print(f"# {location['name']}")
    print("#" * 70 + "\n")

    data = fetch_openmeteo(location)
    if data is None:
        return False

    print("=" * 70)
    print("METADATA")
    print("=" * 70)
    elevation = data.get("elevation")
    print(f"Latitude:  {data.get('latitude')}")
    print(f"Longitude: {data.get('longitude')}")
    print(f"Elevation: {elevation} m (kỳ vọng ~{location['expected_elevation_m']} m)")
    print(f"Timezone:  {data.get('timezone')}")
    if elevation is not None and abs(elevation - location["expected_elevation_m"]) > 100:
        print("⚠️  Elevation lệch >100m so với kỳ vọng - kiểm tra lại grid-cell đã chọn (cell_selection).")

    hourly = data.get("hourly", {})
    hourly_units = data.get("hourly_units", {})
    times = hourly.get("time", [])

    fields_ok = check_field_availability(hourly, hourly_units)
    sanity_ok = check_sanity_ranges(hourly)
    print_sample_rows(hourly, times)

    # Lưu full JSON ra file để inspect nếu cần
    safe_name = location["name"].split(",")[0].strip().lower().replace(" ", "_")
    output_path = f"openmeteo_raw_response_{safe_name}.json"
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"\nĐã lưu full JSON response tại: {os.path.abspath(output_path)}")
    except OSError as e:
        print(f"\n(Không lưu được file JSON: {e} - bỏ qua, không ảnh hưởng kết quả test)")

    print(f"\n>>> KẾT QUẢ {location['name']}: fields_ok={fields_ok}, sanity_ok={sanity_ok}")
    return fields_ok and sanity_ok


if __name__ == "__main__":
    results = {}
    for loc in LOCATIONS:
        results[loc["name"]] = run_test_for_location(loc)

    print("\n" + "=" * 70)
    print("TỔNG KẾT")
    print("=" * 70)
    for name, ok in results.items():
        print(f"{'✅ PASS' if ok else '❌ FAIL':10s} {name}")