import pandas as pd
import geopandas as gpd
import json
from shapely.geometry import shape
import folium

# 1. Đọc dữ liệu của bạn (Giả sử bạn đang load từ DB hoặc CSV)
df = pd.read_json('result_test.json', orient='records')
# Ở đây mình giả lập dataframe dựa trên cấu trúc bạn cung cấp
# df = pd.DataFrame({...})

def visualize_map_coverage(df):
    # 2. Filter lấy dữ liệu của 1 giờ duy nhất
    target_hour = df['observed_at'].dropna().unique()[0]
    df_filtered = df[df['observed_at'] == target_hour].copy()
    
    print(f"Đang vẽ bản đồ cho mốc thời gian: {target_hour}")
    print(f"Số lượng record: {len(df_filtered)}")

    # ---> FIX 1: Ép kiểu cột Timestamp về String để Folium có thể serialize thành JSON <---
    df_filtered['observed_at'] = df_filtered['observed_at'].astype(str)

    # 3. Parse cột polygon_geom
    def parse_geometry(geom_str):
        # ---> FIX 2: Bỏ qua nếu giá trị là NaN (float) để tránh báo lỗi trong console <---
        if pd.isna(geom_str):
            return None
        
        try:
            geom_dict = json.loads(geom_str)
            return shape(geom_dict)
        except Exception as e:
            print(f"Lỗi parse geometry tại chuỗi: {geom_str[:30]}... Chi tiết lỗi: {e}")
            return None

    df_filtered['geometry'] = df_filtered['polygon_geom'].apply(parse_geometry)
    
    # Drop các dòng lỗi parse hoặc bị NaN geometry
    df_filtered = df_filtered.dropna(subset=['geometry'])

    # 4. Convert Pandas DataFrame sang GeoDataFrame
    gdf = gpd.GeoDataFrame(df_filtered, geometry='geometry', crs="EPSG:4326")

    # 5. Khởi tạo Folium map
    m = folium.Map(location=[15.0, 105.0], zoom_start=5, tiles="CartoDB positron")

    # Cấu hình hiển thị tooltip
    # tooltip = folium.GeoJsonTooltip(
    #     fields=['gid_1', 'province_name', 'country_name', 'observed_at'], # Thêm observed_at vào tooltip để check
    #     aliases=['ID:', 'Province:', 'Country:', 'Time:'],
    #     localize=True
    # )
    tooltip = folium.GeoJsonTooltip(
        fields=['gid_1', 'country_name', 'observed_at', 'temperature_2m', 'relative_humidity_2m'],
        aliases=['GID:', 'Country:', 'Time:', 'Temp (°C):', 'Humidity (%):'],
        localize=True
    )

    # Thêm GeoDataFrame vào Map
    folium.GeoJson(
        gdf,
        name="Coverage Check",
        style_function=lambda feature: {
            'fillColor': '#3186cc',
            'color': '#3186cc',
            'weight': 1.5,
            'fillOpacity': 0.4,
        },
        tooltip=tooltip
    ).add_to(m)

    # 6. Lưu ra file HTML
    output_file = "map_coverage_check.html"
    m.save(output_file)
    print(f"Đã lưu bản đồ thành công tại: {output_file}. Bạn mở file này bằng trình duyệt để check nhé!")
# Chạy thử hàm với df của bạn
visualize_map_coverage(df)