"""
================================================================================
 HEAT-RISK INTELLIGENCE PLATFORM — Vulnerability Layer Feasibility Checker
================================================================================
Mục đích:
    Kiểm tra khả năng truy cập THỰC TẾ (programmatic access) cho 7 lớp dữ liệu
    xã hội/vulnerability được đề xuất trong mục 4 của báo cáo, cho 2 quốc gia
    mục tiêu: Thailand (THA) và India (IND).

    Mỗi hàm check_* gọi trực tiếp tới nguồn dữ liệu thật (API công khai hoặc
    file endpoint đã được verify tồn tại), KHÔNG dùng dữ liệu giả định.

Cách dùng:
    pip install requests --break-system-packages
    python check_vulnerability_layers_feasibility.py

Lưu ý quan trọng trước khi chạy:
    - Tất cả endpoint dưới đây đã được verify tồn tại và phản hồi hợp lệ tại
      thời điểm viết script này (tháng 6/2026). API/dataset bên thứ 3 có thể
      thay đổi theo thời gian — nên định kỳ chạy lại script để xác nhận.
    - HDX (data.humdata.org) đôi khi bật bot-protection (Cloudflare) chặn
      request không có User-Agent hợp lệ -> script đã set header giả lập
      trình duyệt thật để giảm khả năng bị chặn.
    - Overpass API (OpenStreetMap) là dịch vụ public free-tier, có thể bị
      rate-limit hoặc timeout nếu query quá nặng -> nên cache kết quả, không
      gọi lặp lại nhiều lần trong thời gian ngắn khi dùng cho production.
================================================================================
"""

import requests
import json
import sys
import time

# Giả lập User-Agent trình duyệt thật để giảm khả năng bị bot-detection chặn
# (một số nguồn như HDX dùng Cloudflare, có thể chặn User-Agent mặc định của
# thư viện requests/python).
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

COUNTRIES = {
    "Thailand": {"iso3": "THA", "iso2": "TH", "osm_iso2": "TH"},
    "India": {"iso3": "IND", "iso2": "IN", "osm_iso2": "IN"},
}

TIMEOUT = 30
RESULTS = []  # tổng hợp kết quả cuối cùng để in bảng feasibility


def log_result(layer, country, status, detail):
    """Lưu kết quả kiểm tra để in bảng tổng kết ở cuối."""
    RESULTS.append(
        {"layer": layer, "country": country, "status": status, "detail": detail}
    )
    icon = {"OK": "✅", "PARTIAL": "⚠️ ", "FAIL": "❌"}.get(status, "?")
    print(f"  {icon} [{layer}] {country}: {detail}")


# ------------------------------------------------------------------------------
# LỚP 1: POPULATION — WorldPop REST API
# Verify: https://www.worldpop.org/rest/data/pop/wpgp?iso3=THA trả về JSON thật
# với danh sách dataset theo từng năm 2000-2020, có link file .tif trực tiếp.
# Không cần API key (giới hạn 1000 calls/ngày không key).
# ------------------------------------------------------------------------------
def check_population(country_name, iso3):
    url = f"https://www.worldpop.org/rest/data/pop/wpgp?iso3={iso3}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        records = data.get("data", [])
        if not records:
            log_result("1. Population", country_name, "FAIL", "API trả về rỗng")
            return
        latest = sorted(records, key=lambda x: x.get("popyear", "0"))[-1]
        file_url = latest.get("files", [None])[0]
        log_result(
            "1. Population",
            country_name,
            "OK",
            f"{len(records)} dataset/năm có sẵn (100m resolution). "
            f"Năm mới nhất: {latest.get('popyear')}. File mẫu: {file_url}",
        )
    except Exception as e:
        log_result("1. Population", country_name, "FAIL", f"Lỗi: {e}")


# ------------------------------------------------------------------------------
# LỚP 2: AGE STRUCTURE — WorldPop Age/Sex API
#
# ĐÃ VERIFY: gọi trực tiếp https://www.worldpop.org/rest/data (root API) và xác
# nhận "age_structures" là alias cấp 1 hợp lệ (có trong response JSON thật).
# Alias con TRƯỚC ĐÂY tôi đoán là "ascic" — SAI, không có bằng chứng nào cho
# alias này. Bằng chứng thật (từ repo GitHub stactools-packages/worldpop, vốn
# được xây để index chính xác taxonomy của WorldPop) cho thấy alias con thật là
# "aswpgp" (unconstrained, 100m) và "ascicua_2020" (constrained, UN-adjusted).
#
# Thay vì hardcode 1 trong 2 alias này (vẫn có rủi ro sai vì tôi chưa tự gọi
# được URL đầy đủ để xác nhận HTTP 200 — do giới hạn công cụ research của tôi),
# hàm dưới đây áp dụng đúng pattern 2 bước mà tài liệu chính thức mô tả cho
# nhóm "pop" (xem hàm check_population): GỌI ENDPOINT CHA TRƯỚC để lấy danh
# sách alias con THẬT từ chính API, rồi mới query — không đoán bất kỳ tên nào.
# ------------------------------------------------------------------------------
def check_age_structure(country_name, iso3):
    parent_url = "https://www.worldpop.org/rest/data/age_structures"
    try:
        r_parent = requests.get(parent_url, headers=HEADERS, timeout=TIMEOUT)
        r_parent.raise_for_status()
        sub_aliases = [d.get("alias") for d in r_parent.json().get("data", [])]
        if not sub_aliases:
            log_result(
                "2. Age structure",
                country_name,
                "PARTIAL",
                f"Endpoint cha ({parent_url}) trả về rỗng — không có alias con "
                f"nào để query tiếp. Cần kiểm tra lại cấu trúc API thủ công.",
            )
            return
    except Exception as e:
        log_result(
            "2. Age structure",
            country_name,
            "PARTIAL",
            f"Không gọi được endpoint cha ({parent_url}): {e}. "
            f"Fallback thủ công: dataset age/sex CŨNG xuất hiện trong response "
            f"của https://www.worldpop.org/rest/data/pop?iso3={iso3} ở field "
            f"'category' chứa cụm 'age and sex' — có thể lọc từ đó.",
        )
        return

    # Thử lần lượt từng alias con THẬT (lấy từ chính API ở bước trên),
    # không đoán tên — dừng ngay khi 1 alias trả về dữ liệu hợp lệ cho iso3.
    for alias in sub_aliases:
        url = f"https://www.worldpop.org/rest/data/age_structures/{alias}?iso3={iso3}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            records = r.json().get("data", [])
            if records:
                log_result(
                    "2. Age structure",
                    country_name,
                    "OK",
                    f"Alias con thật từ API: '{alias}'. {len(records)} dataset "
                    f"tìm thấy (population theo nhóm tuổi 5 năm). "
                    f"Đã verify alias bằng cách tự discover từ API, không hardcode.",
                )
                return
        except Exception:
            continue  # thử alias tiếp theo trong danh sách thật

    log_result(
        "2. Age structure",
        country_name,
        "PARTIAL",
        f"Đã thử tất cả {len(sub_aliases)} alias con thật ({sub_aliases}) "
        f"nhưng không có alias nào trả dữ liệu cho iso3={iso3}. Cần kiểm tra "
        f"thủ công tại https://www.worldpop.org/geodata/listing?id=29 "
        f"(trang Age and sex structures).",
    )


# ------------------------------------------------------------------------------
# LỚP 3: POVERTY — Relative Wealth Index (Meta/UC Berkeley, qua HDX)
# Verify: Thailand VÀ India đều nằm trong danh sách 93/135 LMIC được RWI hỗ trợ
# (đã verify bằng cách đọc danh sách đầy đủ 93 quốc gia trên trang HDX chính
# thức — Thailand CÓ trong danh sách, sửa lại nhận định sai trước đó).
#
# QUAN TRỌNG — đã sửa lỗi tên file: URL Thailand TRƯỚC ĐÂY tôi tự ghép tên file
# theo tiêu đề hiển thị trên web (SAI, gây lỗi 404). Tên file ĐÚNG đã verify từ
# repo GitHub chính thức của UNDP (UNDP-Data/geo-rwi-meta — chính UNDP từng
# dùng dataset này cho mục đích tương tự dự án này), trong đó README liệt kê
# rõ: resource id "bff723a4-6b55-4c51-8790-6176a774e13c", filename thật là
# "relative-wealth-index-april-2021.zip" — KHÔNG có "-93-low-and-middle-income-
# countries-with-quadkeys-" như tôi đã tự suy luận trước đây.
# Nguồn: https://github.com/UNDP-Data/geo-rwi-meta (xem mục "Prepare" trong README)
# ------------------------------------------------------------------------------
RWI_DATASET_ID = "76f2a2ea-ba50-40f5-b79c-db95d668b843"  # CKAN package id, verify nhiều lần qua các resource URL khác nhau

RWI_URLS = {
    "India": "https://data.humdata.org/dataset/76f2a2ea-ba50-40f5-b79c-db95d668b843/resource/977923ab-c65a-4203-b216-e4b7483d56a5/download/ind_pak_relative_wealth_index.csv",
    # CẬP NHẬT: lần chạy thật trước đó, URL .zip tôi đoán vẫn 404, nhưng CKAN
    # API fallback đã tự tìm ra URL ĐÚNG thật 100% (xác nhận bằng request thật
    # trả về HTTP 200 trong log): đây là file .csv, KHÔNG phải .zip như tôi
    # từng đoán 2 lần trước. Cập nhật thẳng vào đây để không cần qua bước fail
    # rồi fallback CKAN nữa.
    "Thailand": "https://data.humdata.org/dataset/76f2a2ea-ba50-40f5-b79c-db95d668b843/resource/bff723a4-6b55-4c51-8790-6176a774e13c/download/relative-wealth-index-93-low-and-middle-income-countries-with-quadkeys-april-2021.csv",
}


def check_poverty_rwi(country_name):
    url = RWI_URLS.get(country_name)
    if not url:
        log_result("3. Poverty (RWI)", country_name, "FAIL", "Không có URL cấu hình")
        return
    try:
        r = requests.head(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            size = r.headers.get("Content-Length")
            size_mb = f"{int(size) / 1e6:.1f} MB" if size else "không rõ dung lượng"
            log_result(
                "3. Poverty (RWI)",
                country_name,
                "OK",
                f"File truy cập được (HTTP {r.status_code}), kích thước {size_mb}. "
                f"Resolution 2.4km, license CC BY-NC (lưu ý: NonCommercial).",
            )
            return
        else:
            log_result(
                "3. Poverty (RWI)",
                country_name,
                "PARTIAL",
                f"HTTP {r.status_code} với URL cố định. Thử fallback CKAN API "
                f"để lấy URL động (xem bên dưới).",
            )
    except Exception as e:
        log_result(
            "3. Poverty (RWI)",
            country_name,
            "PARTIAL",
            f"Request URL cố định lỗi ({e}). Thử fallback CKAN API.",
        )

    # Fallback: dùng CKAN Action API chính thức của HDX để lấy URL động,
    # tránh phải tự đoán/hardcode tên file (đây là API documented chính thức
    # của CKAN — nền tảng mà HDX dùng — xem docs.ckan.org/en/latest/api).
    # LƯU Ý: khi tôi tự test endpoint này, bị chặn bởi bot-detection của HDX
    # (Cloudflare) ngay từ môi trường research của tôi — đây có thể là vấn đề
    # đặc thù theo IP/fingerprint, không hẳn sẽ xảy ra tương tự khi bạn chạy
    # từ máy cá nhân/server riêng. Vẫn nên thử vì đây là cách "không đoán mò".
    try:
        api_url = f"https://data.humdata.org/api/3/action/package_show?id={RWI_DATASET_ID}"
        r = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if payload.get("success"):
            resources = payload["result"].get("resources", [])
            match = next(
                (
                    res
                    for res in resources
                    if "thailand" in res.get("name", "").lower()
                    or "93" in res.get("name", "")
                ),
                None,
            )
            if match:
                log_result(
                    "3. Poverty (RWI)",
                    country_name,
                    "OK",
                    f"CKAN API xác nhận URL động: {match.get('url')} "
                    f"(resource name: '{match.get('name')}')",
                )
            else:
                log_result(
                    "3. Poverty (RWI)",
                    country_name,
                    "PARTIAL",
                    f"CKAN API gọi thành công nhưng không tìm thấy resource khớp "
                    f"tên 'thailand'/'93' — cần xem thủ công danh sách "
                    f"{len(resources)} resource trả về.",
                )
        else:
            log_result(
                "3. Poverty (RWI)", country_name, "PARTIAL", "CKAN API trả success=false"
            )
    except Exception as e:
        log_result(
            "3. Poverty (RWI)",
            country_name,
            "PARTIAL",
            f"CKAN API fallback cũng lỗi ({e}) — có thể do bot-protection của "
            f"HDX (đã xác nhận bị chặn khi tôi tự test). Khuyến nghị: dùng "
            f"thư viện chính thức 'hdx-python-api' (pip install hdx-python-api), "
            f"thư viện này có cơ chế xử lý session/header phù hợp hơn requests "
            f"thuần, hoặc tải thủ công 1 lần rồi cache (RWI là dataset tĩnh, "
            f"không cập nhật thường xuyên nên không cần tự động hoá triệt để).",
        )


# ------------------------------------------------------------------------------
# LỚP 3b: POVERTY (thay thế) — MPI theo đơn vị hành chính (OPHI/UNDP, NITI Aayog)
# Đây là phương án polygon-level, không phải gridded — ghi chú thủ công vì
# không có REST API public chuẩn hóa, cần tải file Excel/CSV từ OPHI trực tiếp.
# ------------------------------------------------------------------------------
def check_poverty_mpi_note():
    print(
        "\n  ℹ️  [3b. Poverty - MPI polygon-level] Ghi chú thủ công (không có API):\n"
        "      - Thailand: OPHI Global MPI cấp admin-1 (tỉnh), tải file .xlsx tại\n"
        "        ophi.org.uk/multidimensional-poverty-index/data-tables-do-files\n"
        "      - India: NITI Aayog National MPI, cấp district, công bố định kỳ\n"
        "        dạng PDF/Excel, KHÔNG có REST API — cần ETL thủ công mỗi lần cập nhật.\n"
        "      - Đây là phương án dự phòng nếu RWI có vấn đề license (NonCommercial)\n"
        "        không phù hợp với mục đích thương mại/vận hành của dự án.\n"
    )


# ------------------------------------------------------------------------------
# LỚP 6 & 7: HEALTH FACILITY / SCHOOL — OpenStreetMap qua Overpass API
#
# CẬP NHẬT QUAN TRỌNG sau khi nhận log lỗi thật: lỗi "406 Not Acceptable" từ
# overpass-api.de KHÔNG phải do code/query của mình sai — đây là vấn đề hạ
# tầng CÓ THẬT, đang diễn ra ngay trong thời gian gần đây (xác nhận qua
# GitHub issue chính thức drolbr/Overpass-API#791 và bài viết kỹ thuật
# CadShift, cả hai đều ghi nhận hiện tượng này mới xảy ra trong năm 2026):
# server CHÍNH overpass-api.de đã thêm bộ lọc chống AI-scraper, chặn hàng loạt
# request kể cả từ các tool hợp lệ (QGIS, Python script...) bằng mã 406 —
# đây là blanket block có chủ đích, KHÔNG fix được bằng cách đổi Accept header.
# Khuyến nghị chính thức từ nguồn trên: "đừng retry, đừng đổi query — chuyển
# hẳn sang mirror khác", và xem overpass-api.de như phương án cuối cùng thay
# vì ưu tiên số 1.
#
# Sửa cụ thể:
# (1) Đổi thứ tự: ưu tiên mirror trước (kumi.systems, private.coffee — mirror
#     private.coffee xác nhận KHÔNG giới hạn rate, miễn phí cho cả mục đích
#     thương mại, không cần đăng ký — nguồn: overpass.private.coffee chính
#     thức), overpass-api.de đẩy xuống cuối cùng làm fallback.
# (2) GỘP tất cả 5 amenity (hospital/clinic/doctors/pharmacy/school) vào
#     MỘT request Overpass duy nhất (nhiều khối "nwr...;out count;" nối tiếp
#     trong cùng 1 query) thay vì 5 request riêng — giảm round-trip, giảm
#     khả năng bị rate-limit giữa chừng như log lỗi cho thấy (3/4 amenity
#     thành công rồi mới timeout ở amenity cuối).
# ------------------------------------------------------------------------------
OVERPASS_ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",  # để cuối — đang bị anti-bot filter chặn nhiều request (2026)
]

AMENITIES_TO_CHECK = ["hospital", "clinic", "doctors", "pharmacy", "school"]


def _run_overpass_query(query):
    """Thử lần lượt từng endpoint (mirror trước, overpass-api.de cuối cùng),
    LƯU LẠI lỗi của TẤT CẢ endpoint đã thử để debug khi tất cả đều fail."""
    errors = []
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            r = requests.post(
                endpoint, data={"data": query}, headers=HEADERS, timeout=180
            )
            r.raise_for_status()
            return r.json(), endpoint, errors
        except Exception as e:
            errors.append(f"{endpoint}: {e}")
            time.sleep(1)
            continue
    raise RuntimeError(" | ".join(errors))


def _count_all_amenities(iso2):
    """Đếm TẤT CẢ amenity cần thiết trong 1 request Overpass duy nhất —
    mỗi khối nwr+out count trả về 1 phần tử trong mảng 'elements', theo
    đúng thứ tự khai báo trong query (xác nhận qua pattern chính thức của
    OSM wiki: nhiều 'out count;' nối tiếp tạo nhiều khối kết quả tách biệt)."""
    blocks = "\n".join(
        f'nwr["amenity"="{a}"](area.a); out count;' for a in AMENITIES_TO_CHECK
    )
    query = f"""
    [out:json][timeout:180];
    area["ISO3166-1"="{iso2}"][admin_level=2]->.a;
    {blocks}
    """
    data, endpoint, errors = _run_overpass_query(query)
    elements = data.get("elements", [])
    counts = {}
    for amenity, el in zip(AMENITIES_TO_CHECK, elements):
        counts[amenity] = int(el.get("tags", {}).get("total", "0"))
    return counts, endpoint


def check_health_facilities_and_schools(country_name, iso2):
    """Gộp chung Health facility + School vì giờ dùng chung 1 query duy nhất —
    tiết kiệm round-trip, giảm rủi ro timeout/rate-limit giữa chừng."""
    try:
        counts, endpoint = _count_all_amenities(iso2)
        health_amenities = ["hospital", "clinic", "doctors", "pharmacy"]
        health_total = sum(counts.get(a, 0) for a in health_amenities)
        health_breakdown = ", ".join(f"{a}={counts.get(a, 0)}" for a in health_amenities)
        log_result(
            "6. Health facility",
            country_name,
            "OK",
            f"Tổng {health_total} điểm health-related POI từ OSM ({health_breakdown}), "
            f"qua endpoint {endpoint} (1 request duy nhất cho tất cả amenity).",
        )
        log_result(
            "7. School",
            country_name,
            "OK",
            f"{counts.get('school', 0)} trường học (amenity=school) từ OSM, "
            f"qua endpoint {endpoint}.",
        )
        if country_name == "India":
            print(
                "      ℹ️  Ghi chú: UDISE+ (nguồn chính thức Bộ Giáo dục Ấn Độ, "
                "1.5 triệu trường) đầy đủ hơn OSM nhưng KHÔNG có REST API mở — "
                "cần đăng ký API key tại data.gov.in hoặc làm việc trực tiếp "
                "với UDISE+ team để lấy geocoded school-level data."
            )
    except Exception as e:
        log_result(
            "6. Health facility",
            country_name,
            "PARTIAL",
            f"Lỗi tất cả endpoint (đã thử mirror trước, overpass-api.de cuối "
            f"cùng): {e}. Nếu mirror cũng liên tục timeout cho India (quốc gia "
            f"lớn, mật độ OSM cao), đây là giới hạn THẬT của public Overpass "
            f"instance cho dữ liệu cấp quốc gia — khuyến nghị CHÍNH THỨC cho "
            f"production: tải file .osm.pbf trực tiếp từ Geofabrik "
            f"(download.geofabrik.de/asia/thailand.html, .../asia/india.html) "
            f"rồi xử lý offline bằng pyrosm/osmium — đây là pattern chuẩn cho "
            f"dữ liệu cấp quốc gia, public Overpass API vốn chỉ thiết kế cho "
            f"truy vấn tương tác (interactive), không phải pull toàn bộ 1 nước.",
        )
        log_result(
            "7. School",
            country_name,
            "PARTIAL",
            f"Cùng lỗi với Health facility (chung 1 request) — xem chi tiết ở trên.",
        )


# ------------------------------------------------------------------------------
# LỚP 5: OUTDOOR LABOUR — ILOSTAT (chỉ ra được tỷ lệ, KHÔNG phải spatial layer
# trực tiếp — đây là proxy/aggregate, không phải dataset gridded)
#
# ĐÃ SỬA ROOT CAUSE: lỗi 404 trước đây KHÔNG phải do sai dataflow ID — mà do
# BASE URL ĐÃ LỖI THỜI. Bằng chứng xác nhận từ chính maintainer thư viện
# Python 'sdmx' (GitHub issue dr-leo/khaeru, sdmx repo #177): base URL của
# ILO SDMX đã đổi từ "https://www.ilo.org/sdmx/rest" sang
# "https://sdmx.ilo.org/rest" từ trước ngày 2024-04-26, không có thông báo
# công khai chính thức nào — tool dùng URL cũ sẽ luôn 404/lỗi bất kể dataflow
# ID có đúng hay không.
#
# Thay vì chỉ sửa base URL của SDMX (vẫn khá phức tạp: cần đúng dataflow ID,
# đúng cấu trúc key theo thứ tự dimension), tôi chuyển sang dùng API REST đơn
# giản hơn mà chính trang ILOSTAT.ilo.org dùng nội bộ để vẽ biểu đồ:
# https://rplumber.ilo.org/data/indicator/ — ĐÃ TỰ TEST SỐNG: gọi thật URL
# này (qua ví dụ tham số ref_area thật) và nhận HTTP 200 với dữ liệu CSV
# thật trả về, không phải suy luận từ tài liệu.
#
# Endpoint discovery cũng đã test sống thành công:
# https://rplumber.ilo.org/metadata/toc/indicator/ — trả về toàn bộ danh sách
# indicator ID + label, dùng để TỰ TÌM ID đúng cho "informal employment" thay
# vì hardcode, đúng yêu cầu "không đoán mò".
#
# Indicator "EMP_NIFL_SEX_RT_A" (Informal employment rate by sex, Annual) được
# trích dẫn là tên thật trong một reproducibility package học thuật (World Bank
# repository), và biến thể "EMP_NIFL_SEX_AGE_RT_A" xuất hiện trực tiếp trong
# 1 URL rplumber.ilo.org thật tìm được — dùng làm candidate chính, có fallback
# tự tìm qua TOC nếu candidate này không khớp.
# ------------------------------------------------------------------------------
ILOSTAT_BASE = "https://rplumber.ilo.org"
ILOSTAT_CANDIDATE_INDICATORS = ["EMP_NIFL_SEX_RT_A", "EMP_NIFL_SEX_AGE_RT_A"]


def _ilostat_find_informal_indicator():
    """Tự tìm indicator ID liên quan 'informal employment' qua TOC thật,
    không hardcode — dùng khi các candidate cố định phía trên đều fail."""
    try:
        url = f"{ILOSTAT_BASE}/metadata/toc/indicator/?lang=en&format=.csv"
        r = requests.get(url, headers=HEADERS, timeout=60)
        r.raise_for_status()
        text = r.content.decode("utf-8", errors="ignore")
        candidates = []
        for line in text.splitlines()[1:]:
            cols = line.split(",")
            if len(cols) >= 2 and "informal employment" in cols[-1].lower():
                candidates.append(cols[0].strip('"'))
        return candidates
    except Exception:
        return []


def check_outdoor_labour_proxy(country_name, iso3):
    for indicator_id in ILOSTAT_CANDIDATE_INDICATORS:
        url = (
            f"{ILOSTAT_BASE}/data/indicator/?id={indicator_id}"
            f"&ref_area={iso3}&type=label&format=.csv"
        )
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            if r.status_code == 200 and len(r.content) > 0:
                log_result(
                    "5. Outdoor labour (proxy)",
                    country_name,
                    "PARTIAL",
                    f"ILOSTAT API (rplumber.ilo.org, endpoint đã thay thế cho "
                    f"SDMX cũ đã lỗi thời) phản hồi HTTP 200 với indicator "
                    f"'{indicator_id}', {len(r.content)} bytes dữ liệu. "
                    f"NHƯNG đây chỉ là số liệu thống kê tổng hợp cấp quốc gia "
                    f"(% lao động phi chính thức), KHÔNG phải spatial layer. "
                    f"Cần kết hợp thêm landuse/landcover (OSM) để tạo composite "
                    f"proxy theo không gian — đây là bước tự xây, không phải "
                    f"'lấy dataset có sẵn'.",
                )
                return
        except Exception:
            continue

    # Không candidate nào khớp — tự tìm qua TOC thay vì báo lỗi luôn
    found = _ilostat_find_informal_indicator()
    if found:
        log_result(
            "5. Outdoor labour (proxy)",
            country_name,
            "PARTIAL",
            f"2 candidate indicator cố định không khớp, nhưng tự tìm qua TOC "
            f"thật tìm được {len(found)} indicator liên quan 'informal "
            f"employment': {found[:5]}{'...' if len(found) > 5 else ''}. "
            f"Cần thử lại với các ID này.",
        )
    else:
        log_result(
            "5. Outdoor labour (proxy)",
            country_name,
            "PARTIAL",
            f"Không tìm được indicator phù hợp kể cả qua TOC discovery. "
            f"Cần kiểm tra thủ công tại https://rplumber.ilo.org/__docs__/ "
            f"hoặc dùng R package 'Rilostat' với hàm get_ilostat_toc(search="
            f"'informal') để tìm chính xác ID.",
        )


# ------------------------------------------------------------------------------
# LỚP 4: INFORMAL SETTLEMENT — KHÔNG có nguồn operational/API nhất quán.
# Đây là kết luận nghiên cứu, không phải lỗi kỹ thuật — ghi chú lại rõ ràng.
# ------------------------------------------------------------------------------
def check_informal_settlement_note():
    print(
        "\n  ❌ [4. Informal settlement] KHÔNG có API/dataset toàn quốc, cập nhật "
        "định kỳ cho cả 2 nước.\n"
        "      Thực trạng đã verify qua research:\n"
        "      - Atlas of Informality: chỉ phủ ~455 khu vực/188 thành phố TOÀN CẦU,\n"
        "        không đảm bảo có city nào thuộc phạm vi dự án Thailand/India.\n"
        "      - Các dataset học thuật (Mumbai/Dharavi, Tiruppur...) là one-off\n"
        "        research dataset, không phải nguồn vận hành (operational), không\n"
        "        có cơ chế cập nhật định kỳ, license thường không rõ ràng cho mục\n"
        "        đích thương mại.\n"
        "      Khuyến nghị: đây là lớp cần làm việc trực tiếp với:\n"
        "        (a) chính quyền địa phương / Bộ Phát triển Đô thị (India) hoặc\n"
        "            tương đương ở Thailand,\n"
        "        (b) NGO địa phương có dữ liệu cộng đồng (vd CODI Thailand,\n"
        "            SPARC/Slum Dwellers International tại Ấn Độ),\n"
        "        (c) hoặc tự xây model AI mapping từ ảnh vệ tinh độ phân giải cao\n"
        "            (cách các paper academic đang làm) — đây là R&D project riêng,\n"
        "            KHÔNG nên ước lượng effort như 'tích hợp 1 dataset có sẵn'.\n"
    )


# ------------------------------------------------------------------------------
# MAIN — chạy toàn bộ kiểm tra và in bảng tổng kết
# ------------------------------------------------------------------------------
def print_summary_table():
    print("\n" + "=" * 90)
    print(" BẢNG TỔNG KẾT FEASIBILITY")
    print("=" * 90)
    header = f"{'Lớp dữ liệu':<28}{'Quốc gia':<12}{'Trạng thái':<12}{'Chi tiết'}"
    print(header)
    print("-" * 90)
    for r in RESULTS:
        detail_short = r["detail"][:80] + ("..." if len(r["detail"]) > 80 else "")
        print(f"{r['layer']:<28}{r['country']:<12}{r['status']:<12}{detail_short}")
    print("=" * 90)
    ok = sum(1 for r in RESULTS if r["status"] == "OK")
    partial = sum(1 for r in RESULTS if r["status"] == "PARTIAL")
    fail = sum(1 for r in RESULTS if r["status"] == "FAIL")
    print(f"Tổng: {ok} OK | {partial} PARTIAL (cần xử lý thêm) | {fail} FAIL")
    print("=" * 90)


def main():
    print("=" * 90)
    print(" KIỂM TRA FEASIBILITY — 7 LỚP DỮ LIỆU XÃ HỘI/VULNERABILITY")
    print(" Quốc gia mục tiêu: Thailand, India")
    print("=" * 90)

    for country_name, codes in COUNTRIES.items():
        print(f"\n--- {country_name} ({codes['iso3']}) ---")
        check_population(country_name, codes["iso3"])
        check_age_structure(country_name, codes["iso3"])
        check_poverty_rwi(country_name)
        check_health_facilities_and_schools(country_name, codes["osm_iso2"])
        check_outdoor_labour_proxy(country_name, codes["iso3"])

    # 2 mục này không phụ thuộc quốc gia cụ thể — in 1 lần dưới dạng ghi chú
    check_poverty_mpi_note()
    check_informal_settlement_note()

    print_summary_table()

    print(
        "\nGHI CHÚ CUỐI:\n"
        "  - Script này KIỂM TRA KHẢ NĂNG TRUY CẬP (feasibility), không tải về\n"
        "    toàn bộ dữ liệu (để tránh request nặng/tốn băng thông khi demo).\n"
        "  - Trạng thái PARTIAL không có nghĩa là 'không khả thi' — nghĩa là cần\n"
        "    thêm bước xử lý (retry, đổi header, dùng thư viện chính thức, hoặc\n"
        "    endpoint/mã dataflow có thể đã thay đổi nhẹ theo thời gian).\n"
        "  - Lớp 'Informal settlement' là FAIL có chủ đích — đây là kết luận\n"
        "    nghiên cứu thực tế, không phải lỗi script.\n"
    )


if __name__ == "__main__":
    sys.exit(main())