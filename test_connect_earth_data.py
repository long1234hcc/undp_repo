import requests
import urllib.parse

# 1. CLASS BẮT BUỘC ĐỂ XỬ LÝ AUTHENTICATION CỦA NASA
# (Phải có class này để tránh bị mất quyền truy cập khi server redirect)
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
            # Nếu bị chuyển hướng ra khỏi máy chủ Earthdata, phải xóa header chứa mật khẩu
            if original != redirect and redirect != 'urs.earthdata.nasa.gov':
                del headers['Authorization']
        return

# ==========================================
# 2. KHỞI TẠO THÔNG TIN
# ==========================================
USERNAME = "lordnanao123"
PASSWORD = "Lordnguyen1234@"

# URL của một file dữ liệu mẫu (hoặc bạn thay bằng URL bạn đang muốn test)
TARGET_URL = "https://opendap.earthdata.nasa.gov/collections/C1748058432-LPCLOUD/granules/MOD11A1.A2025059.h27v07.061.2025060184742"

# Lấy tên file từ URL để lưu vào máy
filename = TARGET_URL.split("/")[-1] 

# ==========================================
# 3. THỰC THI DOWNLOAD
# ==========================================
print(f"[*] Đang kết nối tới NASA Earthdata...")

# Mở session
session = NasaSession(USERNAME, PASSWORD)

# Gửi yêu cầu tải file
response = session.get(TARGET_URL)

# Kiểm tra nếu kết nối thành công (Status code 200)
if response.ok:
    print(f"[*] Kết nối thành công! Đang lưu file: {filename}")
    
    # Ghi nội dung tải được ra file
    with open(filename, 'wb') as f:
        f.write(response.content)
        
    print("[+] Hoàn tất!")
else:
    print(f"[-] Lỗi! Mã lỗi: {response.status_code}")
    print("=> Gợi ý: Kiểm tra lại Username/Password hoặc bạn chưa Approve EULA trên web.")