import pandas as pd
import json
from shapely import wkt
from shapely.geometry import mapping

# ─── Transform WKT → GeoJSON String ──────────────────────────
def transform_geom_to_geojson(wkt_string):
    if pd.isna(wkt_string) or not wkt_string:
        return None
    try:
        geom = wkt.loads(wkt_string)
        geojson = mapping(geom)
        if geojson.get("type") == "MultiPolygon":
            polygons = geojson.get("coordinates", [])
            if len(polygons) == 1:
                geojson = {
                    "type": "Polygon",
                    "coordinates": polygons[0]
                }
        return json.dumps(geojson)
    except Exception as e:
        print(f"[-] Lỗi parse geometry: {e}")
        return None

# ─── Main ────────────────────────────────────────────────────
def main():
    input_file = r"C:\Users\DELL\Downloads\undp_db_public_southeast_asia_districts.json"
    output_file = "distrct_assias.json"

    print("[*] Đang đọc file đầu vào...")
    df = pd.read_json(input_file, orient="records")
    print(f"[+] Đã đọc {len(df):,} dòng.")

    # Drop cột geom cũ (nếu có)
    # if "geom" in df.columns:
    #     df = df.drop(columns=["geom"])

    # Transform cột geom_district thành GeoJSON string
    print("[*] Đang transform geometry...")
    df["geom"] = df["geom"].apply(transform_geom_to_geojson)

    # Drop cột geom cũ
    # df = df.drop(columns=["geom"])

    # Xuất file
    print("[*] Đang xuất file...")
    df.to_json(output_file, orient="records", force_ascii=False)

    print(f"[+] Hoàn tất! File đã lưu tại: {output_file}")

if __name__ == "__main__":
    main()
