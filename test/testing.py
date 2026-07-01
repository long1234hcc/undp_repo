"""
test_gadm_url.py
-----------------
Script test nhanh: verify URL pattern GADM 4.1 GeoJSON-per-level có hoạt động
đúng không, trước khi viết ingest_gadm.py chính thức.

Chạy: python test_gadm_url.py

Lưu ý: Sandbox của Claude bị chặn domain geodata.ucdavis.edu (network whitelist),
nên script này cần chạy trên máy/môi trường có internet bình thường của bạn.
"""

import json

import requests

GADM_BASE = "https://geodata.ucdavis.edu/gadm/gadm4.1/json"


def test_download(iso3: str, level: int) -> None:
    url = f"{GADM_BASE}/gadm41_{iso3}_{level}.json"
    print(f"\n{'='*60}")
    print(f"Testing: {url}")
    print(f"{'='*60}")

    resp = requests.get(url, timeout=30)
    print(f"Status code : {resp.status_code}")
    print(f"Content-Type: {resp.headers.get('content-type')}")
    print(f"Size        : {len(resp.content) / 1024:.1f} KB")

    if resp.status_code != 200:
        print(f"[-] FAIL — không tải được file.")
        return

    # Parse thử xem có phải GeoJSON hợp lệ không
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        print(f"[-] FAIL — không parse được JSON: {e}")
        return

    print(f"GeoJSON type        : {data.get('type')}")
    features = data.get("features", [])
    print(f"Số lượng features    : {len(features)}")

    if not features:
        print("[-] FAIL — không có feature nào.")
        return

    # Check cấu trúc properties của feature đầu tiên — quan trọng nhất là GID
    first_props = features[0].get("properties", {})
    print(f"\nProperties của feature đầu tiên:")
    for k, v in first_props.items():
        print(f"   {k:15s} = {v}")

    # Tìm field GID_{level} — đây là cái user cần (dạng Thai.1.1_1)
    gid_key = f"GID_{level}"
    if gid_key in first_props:
        print(f"\n[+] OK — tìm thấy '{gid_key}' = {first_props[gid_key]}")
    else:
        print(f"\n[-] WARNING — không tìm thấy field '{gid_key}'. "
              f"Keys có sẵn: {list(first_props.keys())}")


if __name__ == "__main__":
    # Test Thailand ADM1 (province) — đúng level mà user hỏi (Thai.1.1_1 là ADM2 thật ra,
    # vì format GID_2 = {GID_1}.{adm2_index}_{version})
    test_download("THA", 1)
    test_download("THA", 2)

    # Test India để chắc chắn pattern áp dụng được cho cả 2 quốc gia trong manifest
    test_download("IND", 1)
    test_download("IND", 2)