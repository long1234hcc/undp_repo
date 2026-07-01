"""
================================================================================
OSM Amenity Dedup — Phien ban chinh thuc (v3)
================================================================================
Lich su thay doi:
    v1 (union-find):
        Loi "bac cau" domino: A-B-C bi gop thanh 1 cum du A-C cach xa.
        Hau qua: pharmacy bi gop nham 44% o nguong 300m.

    v2 (ghep 1-1 don gian):
        Sua loi bac cau: moi node chi ghep voi dung 1 area gan nhat.
        Van con van de: ~20-29% cap ghep co ten KHAC NHAU ro ret
        -> co the la 2 co so that bi ghep nham do chi dua vao khoang cach.

    v3 (phien ban nay) — ghep 1-1 + loc theo ten:
        Bo sung dieu kien: chi ghep khi ten 2 ben TUONG THICH.
        Dinh nghia tuong thich:
            (a) 1 trong 2 ben khong co tag "name" -> chap nhan (khong du
                can cu de bac bo, khong the khang dinh chung la khac nhau)
            (b) Ca 2 deu co ten -> so sau khi chuan hoa (lowercase, bo
                khoang trang thua): neu giong het HOAC 1 cai la substring
                cua cai kia -> chap nhan
            (c) Ca 2 co ten VA ten khac hoan toan -> TU CHOI ghep,
                giu ca 2 ban ghi doc lap (uu tien "tha giu du hon la xoa nham")

        Cac cap bi tu choi do ten khac duoc ghi vao file CSV de review
        thu cong sau neu can.

Nguyen tac thiet ke xuyen suot:
    - Chi gop NODE voi AREA, KHONG gop node-node hay area-area.
      (2 node canh nhau hau het la 2 co so THAT khac nhau, khong phai
       bi map trung — da xac nhan qua do thuc te)
    - Nguong 30m: du lon bao phu sai so GPS/digitizing (thong thuong
      < 10m), du nho de khong gop nham 2 co so sat vach nhau.
    - Greedy matching theo thu tu khoang cach tang dan: cap gan nhat duoc
      ghep truoc khi co tranh chap (1 node/area chi duoc dung 1 lan).
================================================================================
"""

import osmium
import time
import math
import csv
import os

PBF_PATH = r"C:\Users\DELL\Downloads\thailand-260629.osm.pbf"
AMENITIES = {"hospital", "clinic", "doctors", "pharmacy", "school"}
DEDUP_THRESHOLD_M = 30

# File CSV ghi lai cac cap bi tu choi do ten khac nhau, de review thu cong
REJECTED_CSV = "dedup_rejected_name_mismatch.csv"


# ------------------------------------------------------------------------------
# Geometry
# ------------------------------------------------------------------------------

def area_centroid(area):
    """Tinh centroid xap xi cua polygon (trung binh cong cac dinh outer ring).
    Khong phai centroid hinh hoc chinh xac nhung du tot cho muc dich
    nearest-neighbor matching o khoang cach ngan (< 100m)."""
    for ring in area.outer_rings():
        coords = [(n.lon, n.lat) for n in ring if n.location.valid()]
        if coords:
            lon = sum(c[0] for c in coords) / len(coords)
            lat = sum(c[1] for c in coords) / len(coords)
            return lon, lat
    return None


def haversine_m(lon1, lat1, lon2, lat2):
    """Khoang cach Haversine giua 2 diem (don vi: met)."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


# ------------------------------------------------------------------------------
# OSM extraction
# ------------------------------------------------------------------------------

def extract_nodes_and_areas():
    """Quet file .pbf, tach rieng node va area cho tung loai amenity.
    Moi ban ghi luu: (lon, lat, name)."""
    nodes = {a: [] for a in AMENITIES}
    areas = {a: [] for a in AMENITIES}
    print("Dang quet file PBF (NODE + AREA)...")
    start = time.time()
    fp = osmium.FileProcessor(PBF_PATH).with_areas()

    for o in fp:
        if o.is_node():
            amenity = o.tags.get("amenity")
            if amenity in AMENITIES:
                nodes[amenity].append((
                    o.location.lon,
                    o.location.lat,
                    o.tags.get("name", ""),
                ))
        elif o.is_area():
            amenity = o.tags.get("amenity")
            if amenity in AMENITIES:
                c = area_centroid(o)
                if c:
                    areas[amenity].append((
                        c[0], c[1],
                        o.tags.get("name", ""),
                    ))

    elapsed = time.time() - start
    total_nodes = sum(len(v) for v in nodes.values())
    total_areas = sum(len(v) for v in areas.values())
    print(f"Quet xong trong {elapsed:.1f}s | "
          f"Tong: {total_nodes} node, {total_areas} area\n")
    return nodes, areas


# ------------------------------------------------------------------------------
# Name compatibility check
# ------------------------------------------------------------------------------

def names_compatible(name_a, name_b):
    """Kiem tra 2 ten co the la cung 1 co so hay khong.

    Quy tac:
        - 1 trong 2 khong co ten -> True  (khong du can cu de bac bo)
        - Ca 2 co ten, chuan hoa (lower + strip):
            giong het hoac 1 la substring cua 1 -> True
            con lai -> False (ten khac hoan toan, tu choi ghep)
    """
    if not name_a or not name_b:
        return True
    a = name_a.lower().strip()
    b = name_b.lower().strip()
    return a == b or a in b or b in a


# ------------------------------------------------------------------------------
# Dedup core: greedy 1-1 matching voi name filter
# ------------------------------------------------------------------------------

def build_grid(points, cell_deg):
    """Chia khong gian thanh luoi o vuong de tang toc tim kiem lang gieng."""
    grid = {}
    for idx, (lon, lat, _) in enumerate(points):
        key = (int(lon / cell_deg), int(lat / cell_deg))
        grid.setdefault(key, []).append(idx)
    return grid


def match_node_area_1to1(node_points, area_points, threshold_m):
    """Ghep cap 1-1 node-area voi bo loc ten.

    Thuat toan:
        1. Voi moi area, tim tat ca node trong ban kinh threshold_m,
           sap xep theo khoang cach tang dan (nearest first).
        2. Gop tat ca candidate thanh 1 danh sach phang (dist, a_idx, n_idx),
           sap xep theo dist tang dan (xu ly cap gan nhat truoc).
        3. Duyet lan luot: neu ca area_idx va node_idx chua duoc dung,
           VA ten tuong thich -> ghep (matched).
           Neu ten khac nhau ro ret -> ghi vao danh sach rejected, bo qua
           (giu ca 2 ban ghi doc lap).

    Tra ve:
        matched_pairs : list of (dist, area_idx, node_idx) da duoc ghep
        rejected_pairs: list of (dist, area_idx, node_idx) bi tu choi do ten
    """
    cell_deg = threshold_m / 111000
    grid = build_grid(node_points, cell_deg)

    # Buoc 1: tim candidate cho moi area
    all_candidates = []
    for a_idx, (alon, alat, _) in enumerate(area_points):
        cx, cy = int(alon / cell_deg), int(alat / cell_deg)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for n_idx in grid.get((cx + dx, cy + dy), []):
                    nlon, nlat, _ = node_points[n_idx]
                    d = haversine_m(alon, alat, nlon, nlat)
                    if d <= threshold_m:
                        all_candidates.append((d, a_idx, n_idx))

    # Buoc 2: sap xep theo khoang cach tang dan
    all_candidates.sort()

    # Buoc 3: greedy matching voi name filter
    matched_area = set()
    matched_node = set()
    matched_pairs = []
    rejected_pairs = []

    for d, a_idx, n_idx in all_candidates:
        if a_idx in matched_area or n_idx in matched_node:
            continue
        area_name = area_points[a_idx][2]
        node_name = node_points[n_idx][2]
        if not names_compatible(area_name, node_name):
            # Ten khac ro ret -> ghi nhan nhung KHONG ghep, giu ca 2
            rejected_pairs.append((d, a_idx, n_idx))
            # LUU Y: KHONG add vao matched_* -> ca 2 van con "tu do"
            # va co the duoc ghep voi doi tac ten-phu-hop khac sau nay
            continue
        matched_area.add(a_idx)
        matched_node.add(n_idx)
        matched_pairs.append((d, a_idx, n_idx))

    return matched_pairs, rejected_pairs


# ------------------------------------------------------------------------------
# Reporting
# ------------------------------------------------------------------------------

def print_amenity_report(amenity, node_points, area_points,
                          matched_pairs, rejected_pairs):
    n_nodes = len(node_points)
    n_areas = len(area_points)
    total_before = n_nodes + n_areas
    total_after = total_before - len(matched_pairs)

    print(f"=== {amenity} ===")
    print(f"  Truoc dedup : {n_nodes} node + {n_areas} area = {total_before}")
    print(f"  Da ghep (1-1, ten tuong thich): {len(matched_pairs)} cap")
    print(f"  Tu choi (ten khac ro ret)      : {len(rejected_pairs)} cap "
          f"-> giu ca 2 ban ghi doc lap")
    print(f"  Sau dedup   : {total_after}")

    if matched_pairs:
        dists = [d for d, _, _ in matched_pairs]
        print(f"  Khoang cach cap ghep: "
              f"min={min(dists):.1f}m, "
              f"max={max(dists):.1f}m, "
              f"tb={sum(dists)/len(dists):.1f}m")

    if rejected_pairs:
        print(f"  Mau cap bi tu choi (5 cap dau):")
        for d, a_idx, n_idx in rejected_pairs[:5]:
            a_name = area_points[a_idx][2]
            n_name = node_points[n_idx][2]
            print(f"    {d:5.1f}m | area='{a_name}' | node='{n_name}'")
    print()


def save_rejected_csv(all_rejected, node_points_by_amenity,
                       area_points_by_amenity, filepath):
    """Ghi tat ca cap bi tu choi (ten khac) ra CSV de review thu cong."""
    rows = []
    for amenity, rejected in all_rejected.items():
        node_pts = node_points_by_amenity[amenity]
        area_pts = area_points_by_amenity[amenity]
        for d, a_idx, n_idx in rejected:
            alon, alat, a_name = area_pts[a_idx]
            nlon, nlat, n_name = node_pts[n_idx]
            rows.append({
                "amenity": amenity,
                "dist_m": round(d, 1),
                "area_name": a_name,
                "area_lon": round(alon, 6),
                "area_lat": round(alat, 6),
                "node_name": n_name,
                "node_lon": round(nlon, 6),
                "node_lat": round(nlat, 6),
            })
    if not rows:
        print("Khong co cap bi tu choi nao -> khong tao file CSV.")
        return
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Da ghi {len(rows)} cap bi tu choi vao: {os.path.abspath(filepath)}")
    print("(Mo file nay bang Excel/GIS de xem va quyet dinh thu cong)\n")


# ------------------------------------------------------------------------------
# Summary table
# ------------------------------------------------------------------------------

def print_summary(results):
    print("=" * 70)
    print(" TONG KET")
    print("=" * 70)
    print(f"{'Amenity':<12}{'Node':<8}{'Area':<8}{'Truoc':<8}"
          f"{'Ghep':<8}{'TuChoi':<10}{'Sau dedup'}")
    print("-" * 70)
    total_before = total_after = 0
    for amenity, r in results.items():
        before = r["n_nodes"] + r["n_areas"]
        after = before - r["matched"]
        total_before += before
        total_after += after
        print(f"{amenity:<12}{r['n_nodes']:<8}{r['n_areas']:<8}{before:<8}"
              f"{r['matched']:<8}{r['rejected']:<10}{after}")
    print("-" * 70)
    print(f"{'TONG':<12}{'':<8}{'':<8}{total_before:<8}"
          f"{total_before - total_after:<8}{'':<10}{total_after}")
    print("=" * 70)


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------

def main():
    print("=" * 70)
    print(" OSM Amenity Dedup v3 — 1-to-1 matching + name filter")
    print(f" Nguong khoang cach: {DEDUP_THRESHOLD_M}m")
    print("=" * 70 + "\n")

    nodes, areas = extract_nodes_and_areas()

    all_rejected = {}
    results = {}

    for amenity in AMENITIES:
        matched, rejected = match_node_area_1to1(
            nodes[amenity], areas[amenity], DEDUP_THRESHOLD_M
        )
        print_amenity_report(
            amenity, nodes[amenity], areas[amenity], matched, rejected
        )
        all_rejected[amenity] = rejected
        results[amenity] = {
            "n_nodes": len(nodes[amenity]),
            "n_areas": len(areas[amenity]),
            "matched": len(matched),
            "rejected": len(rejected),
        }

    print_summary(results)
    print()
    save_rejected_csv(all_rejected, nodes, areas, REJECTED_CSV)

    print(
        "\nGHI CHU CUOI:\n"
        "  - 'Ghep': cap node-area da xac nhan la trung nhau (gan + ten\n"
        "    tuong thich) -> da bo 1 trong 2, tranh dem trung.\n"
        "  - 'TuChoi': cap gan nhau nhung ten khac ro ret -> GIU CA 2,\n"
        "    uu tien 'tha giu du hon la xoa nham co so that'.\n"
        "  - Xem file CSV de quyet dinh thu cong cac cap bi tu choi.\n"
        "  - De dung ket qua nay trong pipeline: lay tong so 'Sau dedup'\n"
        "    cho moi amenity lam so luong co so chinh thuc.\n"
    )


if __name__ == "__main__":
    main()