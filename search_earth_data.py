import requests

def search_urls():
    # Endpoint API tìm kiếm của NASA
    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"
    
    # Các tham số để lọc (Filter)
    params = {
        "short_name": "MOD11A1",   # Mã dataset (Tôi lấy theo file MOD11A1 bạn vừa tải thành công)
        "version": "061",          # Phiên bản
        "temporal": "2023-01-01T00:00:00Z,2023-01-02T23:59:59Z", # Lọc từ ngày 01 đến ngày 02 tháng 1 năm 2023
        "bounding_box": "102.14,8.56,109.46,23.39", # Tọa độ (Tây, Nam, Đông, Bắc) - Ví dụ đây là khoanh vùng khu vực Việt Nam
        "page_size": 10            # Chỉ lấy thử 10 kết quả đầu tiên để test
    }

    print("[*] Đang gửi yêu cầu tìm kiếm lên CMR API...")
    
    # Gửi request lên server
    response = requests.get(cmr_url, params=params)
    
    # Kiểm tra xem có lỗi không
    if not response.ok:
        print(f"[-] Lỗi API: {response.status_code}")
        return

    # Phân tích dữ liệu JSON trả về
    data = response.json()
    entries = data['feed']['entry']
    
    if len(entries) == 0:
        print("[-] Không tìm thấy dữ liệu nào khớp với điều kiện lọc.")
        return

    print(f"[+] Tìm thấy {len(entries)} file. Đây là các URL để tải:")
    
    # Bóc tách để lấy đúng cái link dùng để download data
    for i, entry in enumerate(entries, 1):
        for link in entry.get('links', []):
            # Lọc link chứa data (thường là file hdf)
            if 'data' in link.get('rel', '') and link.get('href').endswith('.hdf'):
                print(f" {i}. {link['href']}")
                break

# ==========================================
# THỰC THI
# ==========================================
if __name__ == "__main__":
    search_urls()