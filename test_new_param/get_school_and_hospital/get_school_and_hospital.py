"""
osm_amenity_extract.py
======================
Trích xuất danh sách cơ sở y tế và trường học từ file OSM .pbf,
dedup node↔area trùng nhau, xuất ra JSON.

Output:
    amenities.json                   — danh sách cơ sở sau dedup
    dedup_rejected_name_mismatch.csv — cặp gần nhau nhưng tên khác,
                                       cần review thủ công

Schema mỗi bản ghi trong JSON:
    osm_id        str   "node/123" hoặc "area/456"
    amenity       str   hospital | clinic | doctors | pharmacy | school
    name          str   tên chính (tag "name"), "" nếu không có
    name_en       str   tên tiếng Anh (tag "name:en"), "" nếu không có
    lon           float kinh độ
    lat           float vĩ độ
    source_type   str   "node" | "area"
    operator      str   đơn vị vận hành (tag "operator"), "" nếu không có
    addr_province str   tỉnh (tag "addr:province"), "" nếu không có
                        — thường thiếu, nên join từ shapefile admin boundary
    dedup_status  str   "standalone"    — bản ghi độc lập, không có cặp trùng
                        "merged_kept"   — đại diện cho cặp đã ghép (node được giữ)
                        "merged_dropped"— bị loại vì đã có node đại diện (area)

Dedup logic (1-1 bipartite matching, greedy theo khoảng cách):
    - Chỉ ghép node với area, không ghép node-node hay area-area
    - Chỉ ghép khi khoảng cách ≤ DEDUP_THRESHOLD_M (30m)
    - Chỉ ghép khi tên tương thích (xem hàm names_compatible)
    - Khi ghép: node → merged_kept, area → merged_dropped (không vào JSON)
    - Khi tên khác rõ rệt: cả 2 → standalone, ghi vào CSV để review

Yêu cầu:
    pip install osmium --break-system-packages
"""

import csv
import json
import math
import os
import time

import osmium

# ── Config ────────────────────────────────────────────────────────────────────

PBF_PATH            = r"C:\Users\DELL\Downloads\thailand-260629.osm.pbf"
AMENITIES           = {"hospital", "clinic", "doctors", "pharmacy", "school"}
DEDUP_THRESHOLD_M   = 30
OUTPUT_JSON         = "amenities.json"
REJECTED_CSV        = "dedup_rejected_name_mismatch.csv"

# ── Data model ────────────────────────────────────────────────────────────────

def make_record(osm_id, amenity, name, name_en, lon, lat,
                source_type, operator, addr_province,
                dedup_status="standalone"):
    return {
        "osm_id":        osm_id,
        "amenity":       amenity,
        "name":          name,
        "name_en":       name_en,
        "lon":           round(lon, 6),
        "lat":           round(lat, 6),
        "source_type":   source_type,
        "operator":      operator,
        "addr_province": addr_province,
        "dedup_status":  dedup_status,
    }

# ── Geometry ──────────────────────────────────────────────────────────────────

def area_centroid(area):
    """Trả về (lon, lat) trung bình của outer ring, hoặc None."""
    for ring in area.outer_rings():
        coords = [(n.lon, n.lat) for n in ring if n.location.valid()]
        if coords:
            return (
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords),
            )
    return None


def haversine_m(lon1, lat1, lon2, lat2):
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin(math.radians(lat2 - lat1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2)
        * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * R * math.asin(math.sqrt(a))

# ── OSM extraction ────────────────────────────────────────────────────────────

def extract(pbf_path, amenities):
    """
    Quét file .pbf, trả về (nodes, areas) — mỗi cái là dict:
        amenity → list[record]   (record chưa có dedup_status)
    """
    nodes = {a: [] for a in amenities}
    areas = {a: [] for a in amenities}

    print(f"[1/3] Đang quét: {os.path.basename(pbf_path)}")
    t0 = time.time()
    fp = osmium.FileProcessor(pbf_path).with_areas()

    for obj in fp:
        amenity = obj.tags.get("amenity")
        if amenity not in amenities:
            continue

        tags = obj.tags
        name          = tags.get("name", "")
        name_en       = tags.get("name:en", "")
        operator      = tags.get("operator", "")
        addr_province = tags.get("addr:province", "")

        if obj.is_node():
            nodes[amenity].append(make_record(
                osm_id       = f"node/{obj.id}",
                amenity      = amenity,
                name         = name,
                name_en      = name_en,
                lon          = obj.location.lon,
                lat          = obj.location.lat,
                source_type  = "node",
                operator     = operator,
                addr_province= addr_province,
            ))
        elif obj.is_area():
            c = area_centroid(obj)
            if c:
                areas[amenity].append(make_record(
                    osm_id       = f"area/{obj.id}",
                    amenity      = amenity,
                    name         = name,
                    name_en      = name_en,
                    lon          = c[0],
                    lat          = c[1],
                    source_type  = "area",
                    operator     = operator,
                    addr_province= addr_province,
                ))

    n_nodes = sum(len(v) for v in nodes.values())
    n_areas = sum(len(v) for v in areas.values())
    print(f"      Xong trong {time.time() - t0:.1f}s | "
          f"{n_nodes} node, {n_areas} area")
    return nodes, areas

# ── Name compatibility ────────────────────────────────────────────────────────

def names_compatible(a, b):
    """
    True nếu 2 tên có thể là cùng 1 cơ sở:
      - Một bên rỗng → True (không đủ căn cứ bác bỏ)
      - Cả 2 có tên: giống nhau hoặc 1 là substring của 1 → True
      - Cả 2 có tên và khác hoàn toàn → False
    """
    if not a or not b:
        return True
    a, b = a.lower().strip(), b.lower().strip()
    return a == b or a in b or b in a

# ── Dedup ─────────────────────────────────────────────────────────────────────

def _build_grid(records, cell_deg):
    grid = {}
    for idx, rec in enumerate(records):
        key = (int(rec["lon"] / cell_deg), int(rec["lat"] / cell_deg))
        grid.setdefault(key, []).append(idx)
    return grid


def dedup(nodes, areas, threshold_m):
    """
    Ghép cặp 1-1 node↔area theo khoảng cách + tên tương thích.

    Trả về:
        matched_area_indices : set  — area index đã được ghép (sẽ bị drop)
        rejected             : list[(dist, node_rec, area_rec)]
                               — cặp gần nhau nhưng tên khác, để review
    """
    cell_deg = threshold_m / 111_000
    grid = _build_grid(nodes, cell_deg)

    # Tìm tất cả candidate (dist, node_idx, area_idx) trong ngưỡng
    candidates = []
    for a_idx, area_rec in enumerate(areas):
        cx = int(area_rec["lon"] / cell_deg)
        cy = int(area_rec["lat"] / cell_deg)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n_idx in grid.get((cx + dx, cy + dy), []):
                    d = haversine_m(
                        area_rec["lon"], area_rec["lat"],
                        nodes[n_idx]["lon"], nodes[n_idx]["lat"],
                    )
                    if d <= threshold_m:
                        candidates.append((d, n_idx, a_idx))

    candidates.sort()

    matched_nodes  = set()
    matched_areas  = set()
    rejected       = []

    for d, n_idx, a_idx in candidates:
        if n_idx in matched_nodes or a_idx in matched_areas:
            continue
        if not names_compatible(nodes[n_idx]["name"], areas[a_idx]["name"]):
            rejected.append((d, nodes[n_idx], areas[a_idx]))
            # Không đánh dấu "đã dùng" → cả 2 vẫn có thể ghép với đối tác khác
            continue
        matched_nodes.add(n_idx)
        matched_areas.add(a_idx)

    return matched_areas, rejected

# ── Build final record list ───────────────────────────────────────────────────

def build_output(nodes_by_amenity, areas_by_amenity):
    """
    Chạy dedup cho từng amenity, gán dedup_status, gộp thành 1 list duy nhất.
    Area bị ghép (merged_dropped) không vào output.
    """
    print("[2/3] Đang dedup...")
    all_records  = []
    all_rejected = []
    stats        = {}

    for amenity in sorted(nodes_by_amenity):
        node_recs = nodes_by_amenity[amenity]
        area_recs = areas_by_amenity[amenity]

        matched_area_idxs, rejected = dedup(node_recs, area_recs, DEDUP_THRESHOLD_M)

        # Tìm node nào là đại diện cho cặp đã ghép
        # (rebuild từ matched_area_idxs → matched_node_idxs)
        cell_deg = DEDUP_THRESHOLD_M / 111_000
        grid = _build_grid(node_recs, cell_deg)
        matched_node_idxs = set()
        for a_idx in matched_area_idxs:
            area_rec = area_recs[a_idx]
            cx = int(area_rec["lon"] / cell_deg)
            cy = int(area_rec["lat"] / cell_deg)
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for n_idx in grid.get((cx + dx, cy + dy), []):
                        if n_idx in matched_node_idxs:
                            continue
                        d = haversine_m(
                            area_rec["lon"], area_rec["lat"],
                            node_recs[n_idx]["lon"], node_recs[n_idx]["lat"],
                        )
                        if d <= DEDUP_THRESHOLD_M and names_compatible(
                            node_recs[n_idx]["name"], area_rec["name"]
                        ):
                            if best is None or d < best[0]:
                                best = (d, n_idx)
            if best:
                matched_node_idxs.add(best[1])

        # Gán dedup_status cho node
        for n_idx, rec in enumerate(node_recs):
            rec = dict(rec)
            rec["dedup_status"] = (
                "merged_kept" if n_idx in matched_node_idxs else "standalone"
            )
            all_records.append(rec)

        # Gán dedup_status cho area — bỏ merged_dropped khỏi output
        n_dropped = 0
        for a_idx, rec in enumerate(area_recs):
            if a_idx in matched_area_idxs:
                n_dropped += 1
                continue  # không thêm vào output
            rec = dict(rec)
            rec["dedup_status"] = "standalone"
            all_records.append(rec)

        all_rejected.extend(rejected)
        stats[amenity] = {
            "nodes":   len(node_recs),
            "areas":   len(area_recs),
            "merged":  len(matched_area_idxs),
            "rejected": len(rejected),
            "total":   len(node_recs) + len(area_recs) - n_dropped,
        }

    return all_records, all_rejected, stats

# ── Save outputs ──────────────────────────────────────────────────────────────

def save_json(records, filepath):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    print(f"      → {os.path.abspath(filepath)}")
    print(f"        {len(records)} bản ghi")


def save_rejected_csv(rejected, filepath):
    if not rejected:
        return
    rows = [
        {
            "dist_m":         round(d, 1),
            "node_osm_id":    n["osm_id"],
            "node_name":      n["name"],
            "node_lon":       n["lon"],
            "node_lat":       n["lat"],
            "area_osm_id":    a["osm_id"],
            "area_name":      a["name"],
            "area_lon":       a["lon"],
            "area_lat":       a["lat"],
            "amenity":        n["amenity"],
        }
        for d, n, a in rejected
    ]
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"      → {os.path.abspath(filepath)}")
    print(f"        {len(rows)} cặp cần review thủ công")


def print_summary(stats):
    SEP = "=" * 62
    print(SEP)
    print(f"{'Amenity':<12}{'Node':<7}{'Area':<7}"
          f"{'Ghép':<7}{'TừChối':<9}{'Output'}")
    print("-" * 62)
    total = 0
    for amenity, s in stats.items():
        print(f"{amenity:<12}{s['nodes']:<7}{s['areas']:<7}"
              f"{s['merged']:<7}{s['rejected']:<9}{s['total']}")
        total += s["total"]
    print("-" * 62)
    print(f"{'TỔNG':<12}{'':<7}{'':<7}{'':<7}{'':<9}{total}")
    print(SEP)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 62)
    print(f" OSM Amenity Extract | ngưỡng {DEDUP_THRESHOLD_M}m")
    print("=" * 62)

    nodes, areas = extract(PBF_PATH, AMENITIES)
    records, rejected, stats = build_output(nodes, areas)

    print(f"[3/3] Đang ghi output...")
    save_json(records, OUTPUT_JSON)
    save_rejected_csv(rejected, REJECTED_CSV)

    print()
    print_summary(stats)


if __name__ == "__main__":
    main()