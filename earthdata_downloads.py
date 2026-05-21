import requests
import urllib.parse
import os

# ==========================================
# 1. CLASS AUTH CỦA NASA (Từ Step 1)
# ==========================================
class NasaSession(requests.Session):
    def __init__(self, username, password):
        super().__init__()
        self.auth = (username, password)

    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        url = prepared_request.url
        if 'Authorization' in headers:
            original = urllib.parse.urlparse(response.request.url).hostname
            redirect = urllib.parse.urlparse(url).hostname
            if original != redirect and redirect != 'urs.earthdata.nasa.gov':
                del headers['Authorization']
        return

# ==========================================
# 2. HÀM TÌM KIẾM (Từ Step 2)
# ==========================================
def get_download_urls():
    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"
    params = {
        "short_name": "HLSL30",  # CHUYỂN SANG DÙNG LANDSAT CHO TẤT CẢ
        "version": "2.0",        # HLS hiện tại đang dùng version 2.0
        # "temporal": "2023-01-01T00:00:00Z,2023-01-02T23:59:59Z", # (Sửa lại ngày của bạn)
        "temporal": "2023-01-01T00:00:00Z,2023-01-31T23:59:59Z",
        "bounding_box": "100.3,13.5,100.9,14.0",                 # (Sửa lại box của bạn)
        "cloud_cover": "0,30",
        "page_size": 20
    }
    
    print("[*] Đang tìm kiếm URL từ hệ thống NASA...")
    response = requests.get(cmr_url, params=params)
    
    if not response.ok:
        print(f"[-] Lỗi API: {response.status_code}")
        return []

    # entries = response.json()['feed']['entry']
    entries = response.json()['feed']['entry']
    urls = []
    
    target_bands = ('B04.tif', 'B05.tif', 'B10.tif') 
    
    for entry in entries:
        for link in entry.get('links', []):
            href = link.get('href', '')
            # Chỉ bóc tách những link là file data (.tif) VÀ kết thúc bằng B04, B05 hoặc B10
            if 'data' in link.get('rel', '') and href.endswith(target_bands):
                urls.append(href)
                
    return urls
    # Bóc tách link .hdf cho vào mảng (list)
    # for entry in entries:
    #     for link in entry.get('links', []):
    #         if 'data' in link.get('rel', '') and link.get('href').endswith('.hdf'):
    #             urls.append(link['href'])
    #             break
                
    # return urls

# ==========================================
# 3. THỰC THI PIPELINE (STEP 1 + STEP 2)
# ==========================================
if __name__ == "__main__":
    USERNAME = "lordnanao123"
    PASSWORD = "Lordnguyen1234@"
    
    # Tạo thư mục chứa dữ liệu
    SAVE_DIR = "earthdata_downloads"
    os.makedirs(SAVE_DIR, exist_ok=True)
    
    # Lấy danh sách URL
    urls_to_download = get_download_urls()
    
    if urls_to_download:
        print(f"[+] Tìm thấy {len(urls_to_download)} file. Bắt đầu tải...\n")
        
        # Khởi tạo session đăng nhập (chỉ cần làm 1 lần)
        session = NasaSession(USERNAME, PASSWORD)
        
        # Vòng lặp tải từng file
        for index, url in enumerate(urls_to_download, 1):
            filename = url.split("/")[-1]
            save_path = os.path.join(SAVE_DIR, filename)
            
            # Kiểm tra file đã tải chưa
            if os.path.exists(save_path):
                print(f"[{index}/{len(urls_to_download)}] File {filename} đã tồn tại. Bỏ qua.")
                continue
            
            print(f"[{index}/{len(urls_to_download)}] Đang tải: {filename}...")
            
            # Tải theo từng chunk (1MB) để tránh tràn RAM
            response = session.get(url, stream=True)
            if response.ok:
                with open(save_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        f.write(chunk)
                print(f"  -> Tải thành công!")
            else:
                print(f"  -> Lỗi khi tải ({response.status_code})")
                
        print("\n[+] HOÀN TẤT TOÀN BỘ QUÁ TRÌNH TẢI DỮ LIỆU!")
    else:
        print("[-] Không có file nào để tải.")