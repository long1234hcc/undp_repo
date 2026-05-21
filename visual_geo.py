"""
visualize_bangkok.py  (refactored)
────────────────────────────────────────────────────────────────
Vẽ temperature_2m theo polygon district (geom_district) thay vì
bounding box grid.

Thay đổi chính so với v1:
  - geometry lấy từ cột geom_district (WKT) thay vì tạo box()
  - Tooltip bổ sung district_name, province_name, gid_2
  - Circle marker màu đỏ nếu gid_2 IS NULL (chưa assign district)
  - Aggregate: nếu nhiều grid cell cùng gid_2 → lấy mean temperature

pip install pandas geopandas folium shapely branca
────────────────────────────────────────────────────────────────
"""

import json
import pandas as pd
import geopandas as gpd
import folium
from shapely import wkt
from shapely.geometry import mapping
from branca.colormap import linear


# ── 1. Load data ──────────────────────────────────────────────
df = pd.read_json("district_test_visual.json", orient="records")

print(f"Tổng records  : {len(df)}")
print(f"Columns       : {list(df.columns)}")


# ── 2. Lấy 1 snapshot thời gian duy nhất ─────────────────────
target_date = df["observed_at"].dropna().unique()[0]
df_snap = df[df["observed_at"] == target_date].copy()
df_snap["observed_at"] = df_snap["observed_at"].astype(str)
print(f"Snapshot      : {target_date}  |  {len(df_snap)} grid cells")


# ── 3. Tách thành 2 nhóm ─────────────────────────────────────
#   A) Có geom_district → vẽ polygon district
#   B) Không có geom_district (gid_2 IS NULL) → vẽ circle marker

df_with    = df_snap[df_snap["geom_district"].notna()].copy()
df_without = df_snap[df_snap["geom_district"].isna()].copy()

print(f"  Có district  : {len(df_with)}")
print(f"  Không có     : {len(df_without)}")


# ── 4. Build GeoDataFrame từ geom_district (WKT) ─────────────
#   Aggregate: nếu nhiều grid cell → cùng gid_2, lấy mean temp/humidity
#   (giữ geom_district và metadata của row đầu tiên trong group)

agg_dict = {
    "temperature_2m":       "mean",
    "relative_humidity_2m": "mean",
    "temp_nor":             "mean",
    "humidity_nor":         "mean",
    "lat_center":           "first",
    "lon_center":           "first",
    "observed_at":          "first",
    "district_name":        "first",
    "province_name":        "first",
    "gid_1":                "first",
    "geom_district":        "first",   # WKT giống nhau trong cùng gid_2
}

df_agg = (
    df_with
    .groupby("gid_2", as_index=False)
    .agg(agg_dict)
)

print(f"  Sau aggregate: {len(df_agg)} unique districts")

# Parse WKT → Shapely geometry
df_agg["geometry"] = df_agg["geom_district"].apply(wkt.loads)

gdf = gpd.GeoDataFrame(df_agg, geometry="geometry", crs="EPSG:4326")


# ── 5. Colormap theo nhiệt độ ─────────────────────────────────
temp_min = gdf["temperature_2m"].min()
temp_max = gdf["temperature_2m"].max()
print(f"  Temp range   : {temp_min:.2f} – {temp_max:.2f} °C")

colormap = linear.YlOrRd_09.scale(temp_min, temp_max)
colormap.caption = "Temperature 2m (°C)"


# ── 6. Khởi tạo map ───────────────────────────────────────────
bangkok_center = [13.75, 100.52]
m = folium.Map(
    location=bangkok_center,
    zoom_start=10,
    tiles="CartoDB positron",
)


# ── 7. Vẽ district polygons ───────────────────────────────────
def style_fn(feature):
    temp = feature["properties"].get("temperature_2m")
    fill = colormap(temp) if temp is not None else "#cccccc"
    return {
        "fillColor":   fill,
        "color":       "#444444",
        "weight":      1.2,
        "fillOpacity": 0.70,
    }

def highlight_fn(feature):
    return {
        "fillOpacity": 0.90,
        "weight":      2.5,
        "color":       "#222222",
    }

tooltip_district = folium.GeoJsonTooltip(
    fields=[
        "district_name", "province_name", "gid_2",
        "temperature_2m", "relative_humidity_2m",
        "temp_nor", "humidity_nor",
        "lat_center", "lon_center", "observed_at",
    ],
    aliases=[
        "District:", "Province:", "GID_2:",
        "Temp (°C):", "Humidity (%):",
        "Temp norm:", "Humidity norm:",
        "Lat:", "Lon:", "Date:",
    ],
    localize=True,
    sticky=True,
    style=(
        "background-color: white; color: #333; "
        "font-family: monospace; font-size: 12px; "
        "padding: 8px; border-radius: 4px;"
    ),
)

folium.GeoJson(
    gdf,
    name="District Temperature",
    style_function=style_fn,
    highlight_function=highlight_fn,
    tooltip=tooltip_district,
).add_to(m)


# ── 8. Dot tại tâm mỗi grid cell ─────────────────────────────
#   Đen  = có district
#   Đỏ   = NULL gid_2 (ngoài ranh giới)

for _, row in gdf.iterrows():
    folium.CircleMarker(
        location=[row["lat_center"], row["lon_center"]],
        radius=4,
        color="black",
        fill=True,
        fill_color="white",
        fill_opacity=0.9,
        weight=1.5,
        tooltip=(
            f"({row['lat_center']}, {row['lon_center']}) "
            f"| {row['temperature_2m']:.1f}°C "
            f"| {row['district_name']} ({row['gid_2']})"
        ),
    ).add_to(m)

# Grid cells không có district
for _, row in df_without.iterrows():
    folium.CircleMarker(
        location=[row["lat_center"], row["lon_center"]],
        radius=5,
        color="red",
        fill=True,
        fill_color="red",
        fill_opacity=0.8,
        weight=1.5,
        tooltip=(
            f"({row['lat_center']}, {row['lon_center']}) "
            f"| {row.get('temperature_2m', 'N/A')}°C "
            f"| gid_2: NULL"
        ),
    ).add_to(m)


# ── 9. Legend ─────────────────────────────────────────────────
legend_html = """
<div style="
    position: fixed; bottom: 40px; left: 40px; z-index: 1000;
    background: white; padding: 12px 16px; border-radius: 8px;
    border: 1px solid #ddd; font-size: 13px; line-height: 2.0;
    box-shadow: 2px 2px 8px rgba(0,0,0,0.15);
    font-family: monospace;
">
    <b>Grid Points</b><br>
    <span style="color:black">●</span> Đã assign district<br>
    <span style="color:red">●</span> NULL — ngoài ranh giới
</div>
"""
m.get_root().html.add_child(folium.Element(legend_html))

colormap.add_to(m)
folium.LayerControl().add_to(m)


# ── 10. Lưu ──────────────────────────────────────────────────
output = "map_district_temperature.html"
m.save(output)
print(f"✓ Saved → {output}")
print()
print("Kiểm tra nhanh:")
print(gdf[["gid_2", "district_name", "province_name", "temperature_2m"]].to_string(index=False))