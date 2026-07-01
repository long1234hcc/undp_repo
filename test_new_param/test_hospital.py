"""
Muc dich: tra loi cau hoi "Neu chi lay NODE (bo qua AREA), co bi mat du
lieu dang ke khong?" cho tung loai amenity.

Logic:
    Voi moi AREA (polygon building da gan tag amenity), tim NODE gan nhat
    cung loai amenity trong ban kinh test_radius_m.
    - Neu tim thay node trong ban kinh -> area nay "co node tuong duong",
      bo no di khong mat thong tin (vi node da dai dien roi).
    - Neu KHONG tim thay node nao trong ban kinh -> area nay la "area-only",
      neu chi lay node se MAT HOAN TOAN co so nay khoi du lieu.

Khac voi script dedup truoc (dung union-find, de bi loi "bac cau" khi gop
nhieu diem lien tiep thanh 1 cum), o day chi lam 1 viec don gian: voi MOI
area, tim node GAN NHAT (nearest-neighbor, bat cap 1-1), khong cho phep
bac cau qua area/node khac. Day la cach do dung cau hoi "mat bao nhieu %
neu chi lay node", khong bi nhieu boi hieu ung chuoi domino.

Output: voi moi amenity, in ra:
    - Tong so node
    - Tong so area
    - So area CO node gan (trong ban kinh) -> an toan neu bo area
    - So area KHONG co node gan -> se MAT neu chi lay node
    - % area se mat, ung voi nhieu ban kinh khac nhau de xem do nhay
"""

import osmium
import math

PBF_PATH = r"C:\Users\DELL\Downloads\thailand-260629.osm.pbf"
AMENITIES = {"hospital", "clinic", "doctors", "pharmacy", "school"}
TEST_RADII_M = [25, 50, 100, 150, 200, 300]


def area_centroid(area):
    for ring in area.outer_rings():
        coords = [(n.lon, n.lat) for n in ring if n.location.valid()]
        if coords:
            lon = sum(c[0] for c in coords) / len(coords)
            lat = sum(c[1] for c in coords) / len(coords)
            return lon, lat
    return None


def haversine_m(lon1, lat1, lon2, lat2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def extract_nodes_and_areas():
    nodes = {a: [] for a in AMENITIES}
    areas = {a: [] for a in AMENITIES}
    print("Dang quet toan bo file (NODE + AREA)...")
    fp = osmium.FileProcessor(PBF_PATH).with_areas()

    for o in fp:
        if o.is_node():
            amenity = o.tags.get("amenity")
            if amenity in AMENITIES:
                nodes[amenity].append((o.location.lon, o.location.lat))
        elif o.is_area():
            amenity = o.tags.get("amenity")
            if amenity in AMENITIES:
                c = area_centroid(o)
                if c:
                    areas[amenity].append(c)

    print("Quet xong.\n")
    return nodes, areas


def build_grid(points, cell_deg):
    """Bucket cac diem theo o luoi de tang toc tim kiem lan can,
    giong cach lam o script dedup truoc."""
    grid = {}
    for idx, (lon, lat) in enumerate(points):
        key = (int(lon / cell_deg), int(lat / cell_deg))
        grid.setdefault(key, []).append(idx)
    return grid


def nearest_node_distance(area_pt, node_points, grid, cell_deg):
    """Tim khoang cach toi node GAN NHAT (khong bac cau), tra ve None
    neu khong co node nao trong cac o lan can."""
    lon, lat = area_pt
    cx, cy = int(lon / cell_deg), int(lat / cell_deg)
    best = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for idx in grid.get((cx + dx, cy + dy), []):
                nlon, nlat = node_points[idx]
                d = haversine_m(lon, lat, nlon, nlat)
                if best is None or d < best:
                    best = d
    return best


def analyze_amenity(amenity, node_points, area_points):
    n_nodes = len(node_points)
    n_areas = len(area_points)

    if n_areas == 0:
        print(f"=== {amenity} ===")
        print(f"  Node: {n_nodes} | Area: 0 -> khong co area nao, "
              f"chi lay node KHONG MAT gi (vi von da khong co area).\n")
        return

    print(f"=== {amenity} (Node: {n_nodes} | Area: {n_areas}) ===")
    print(f"{'Ban kinh(m)':<14}{'Area co node gan':<20}{'Area KHONG co node gan':<25}{'%se mat neu bo area'}")

    # dung ban kinh lon nhat de build grid 1 lan, danh gia o nhieu nguong
    max_r = max(TEST_RADII_M)
    cell_deg = max_r / 111000
    grid = build_grid(node_points, cell_deg)

    # tinh truoc khoang cach gan nhat cho moi area (1 lan), roi so sanh
    # voi tung nguong -> khong can lap lai tim kiem nhieu lan
    nearest_dists = [
        nearest_node_distance(pt, node_points, grid, cell_deg)
        for pt in area_points
    ]

    for r in TEST_RADII_M:
        has_nearby = sum(1 for d in nearest_dists if d is not None and d <= r)
        no_nearby = n_areas - has_nearby
        pct_lost = no_nearby / n_areas * 100
        print(f"{r:<14}{has_nearby:<20}{no_nearby:<25}{pct_lost:.1f}")
    print()


def main():
    nodes, areas = extract_nodes_and_areas()
    for a in AMENITIES:
        analyze_amenity(a, nodes[a], areas[a])

    print(
        "GHI CHU DOC KET QUA:\n"
        "  - Cot 'Area KHONG co node gan' = so co so se MAT TRANG neu ban\n"
        "    quyet dinh chi lay node, bo area.\n"
        "  - Neu % nay gan 0 o ban kinh hop ly (vd 50-100m) -> chi lay node\n"
        "    la an toan, don gian hoa duoc.\n"
        "  - Neu % nay dang ke (vd >5-10%) -> khong nen bo area, vi se mat\n"
        "    that su mot phan co so y te/truong hoc khoi du lieu, khong phai\n"
        "    chi la van de dedup.\n"
        "  - Luu y: day la phep do 1-chieu (tu area tim ve node gan nhat),\n"
        "    khong bi loi 'bac cau' nhu thuat toan union-find dedup truoc do.\n"
    )


if __name__ == "__main__":
    main()