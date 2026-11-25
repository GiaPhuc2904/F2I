# Cộng sự Kho & Công nợ Offline — Dành cho MISA Kế toán

Ứng dụng web chạy **100% không cần mạng (Offline)** ngay trên máy tính Windows tại kho FMCG. Chủ kho chỉ cần kéo thả 1 tệp **`.zip`** chứa các sổ Excel xuất từ MISA Kế toán để xem ngay:
1. **Tổng quan kho & lãi thực tế** *(đã trừ giá vốn hàng xuất tặng 0đ)*.
2. **Khám bệnh kho & Gợi ý từng mặt hàng** *(món bán chạy, món lời nhiều, món hay hết hàng, món nằm im lâu / hàng KM dư, số thùng nên đặt nhập tuần tới)*.
3. **Dự báo sức bán (11 cách dự báo + Tự động ghép tốt nhất)** *(đổi cách dự báo cập nhật tức thì 0 giây, có giải thích tiếng Việt bình dân, xuất bảng đặt hàng `.csv` mở thẳng bằng Excel)*.
4. **Công nợ khách hàng & 3 Biểu đồ dòng tiền** *(chia 4 nhóm: Cần gọi đòi ngay · Hay trả chậm nhưng có trả · Khách mới · Khách trả rất đều)*.

> **An toàn & Bảo mật tuyệt đối:**
> - Chạy hoàn toàn trên máy nội bộ (`http://127.0.0.1:8050`), **không đăng nhập**, **không gửi 1 byte dữ liệu nào ra ngoài Internet**.
> - Có sẵn công tắc **"Che tên & tiền"** ở góc trên bên phải: bấm 1 chạm tự động đổi toàn bộ tên khách, tên hàng, tên kho sang mã ẩn danh (`KH_0001`, `SKU_0001`, `KHO_01`) và ẩn số tiền VNĐ để thoải mái chụp ảnh hoặc quay màn hình.

---

## 1. Cài đặt lần đầu trên Windows (Chỉ làm 1 lần duy nhất)

1. Cài đặt **Python 3.11 hoặc 3.12** trên máy tính Windows (lúc cài nhớ tích chọn ô **`Add Python to PATH`**).
2. Tải thư mục dự án này về máy chủ của kho (ví dụ giải nén ra `D:\CongSuKhoMISA`).
3. Nhấp đúp chuột chạy tệp **`1_Cai_Dat_Lan_Dau.bat`**:
   - **Nếu máy đang có mạng lần đầu:** chương trình tự tạo `.venv` và tải thư viện cần thiết trong ít phút.
   - **Nếu máy kho không nối mạng ngay từ đầu:** trên một máy có mạng khác, chạy `python scripts/prepare_offline_wheels.py` để tải trọn bộ gói cài vào thư mục `offline_wheels\`, chép kèm vào USB mang sang máy kho rồi nhấp đúp `1_Cai_Dat_Lan_Dau.bat` — ứng dụng sẽ tự cài 100% không cần mạng.

---

## 2. Cách mở dùng hàng ngày (Tắt Wi-Fi vẫn chạy bình thường)

1. Có thể **tắt hẳn Wi-Fi / rút dây mạng** để kiểm chứng.
2. Nhấp đúp chuột vào tệp **`2_Mo_App_Offline.bat`**.
3. Trình duyệt sẽ tự động mở trang **`http://127.0.0.1:8050`** *(giữ cửa sổ đen thu nhỏ dưới thanh công cụ trong lúc xem báo cáo)*.
4. Nếu chưa có sẵn tệp MISA, bấm nút **"Dùng thử dữ liệu mẫu (11 tháng)"** ngay trên màn hình để xem thử toàn bộ tính năng.

---

## 3. Cách xuất 4 sổ Excel chính từ MISA Kế toán và nén thành 1 tệp `.zip`

Từ phần mềm MISA SME / AMIS, chỉ cần xuất ra Excel (`.xlsx`) **4 báo cáo chính** trong kỳ cần xem (không cần xuất *Sổ chi tiết vật tư hàng hóa*):

1. **Danh sách hàng hóa, dịch vụ** (`Danh_sach_hang_hoa_dich_vu.xlsx`) — để lấy toàn bộ danh mục mặt hàng, quy cách đổi ra Thùng và giá mua/bán.
2. **Sổ chi tiết mua hàng** (`So_chi_tiet_mua_hang.xlsx`) — để tính dòng tiền chi nhập hàng và theo dõi đợt nhập gần nhất của từng món.
3. **Sổ chi tiết bán hàng** (`So_chi_tiet_ban_hang.xlsx`) — để dự báo sức bán, tách hàng tặng 0đ và tính lãi gộp thực.
4. **Chi tiết công nợ phải thu khách hàng — TK 131** (`Chi_tiet_cong_no_phai_thu_khach_hang.xlsx`) — để chấm 4 nhóm công nợ và vẽ đủ 3 biểu đồ dòng tiền.
   - *Tùy chọn thêm:* Nếu bạn có xuất kèm bảng **Tổng hợp tồn kho** (`Tong_hop_ton_kho.xlsx`) cho toàn kho thì bỏ chung vào `.zip` để xem thêm tồn cuối kỳ tuyệt đối; nếu không có (hoặc file vật tư chỉ lọc vài mã lẻ), hệ thống tự động đối chiếu trực tiếp **Đợt nhập mua – Xuất bán** cho 100% mặt hàng mà không báo thiếu sổ!

**Cách nén:** Bôi đen các tệp `.xlsx` vừa xuất → Chuột phải → **Send to → Compressed (zipped) folder** (tạo thành 1 tệp `.zip` duy nhất) → Kéo thả thẳng tệp `.zip` đó vào ứng dụng!

---

## 4. Cách cập nhật hàng tuần / hàng tháng (Không bao giờ bị trùng dòng)

- Mỗi tuần hoặc mỗi tháng, bạn xuất các sổ MISA của tháng mới (hoặc xuất gối đầu thêm vài tuần trước đó để bắt các đơn vừa thu thêm tiền), nén thành `.zip`.
- Bấm nút **"Nạp file MISA (.zip)"** ở góc trên phải → Giữ nguyên lựa chọn mặc định **"Cập nhật thêm tháng mới (Khuyên dùng)"** → Kéo thả tệp `.zip` vào.
- Ứng dụng tự động **giữ nguyên 100% lịch sử các tháng/năm cũ**, cập nhật lại các đơn mới chỉnh sửa hoặc vừa thu xong nợ, và **đảm bảo 0% trùng lặp dòng** ngay cả khi bạn lỡ tay kéo thả lại cùng một tệp `.zip` nhiều lần.

---

## 5. Dành cho Kỹ thuật / Kiểm thử trên Linux

```bash
# Khởi tạo môi trường và chạy ứng dụng tại http://127.0.0.1:8050
./run_linux.sh

# Chạy toàn bộ bộ kiểm thử tự động (100% dữ liệu giả lập trong thư mục tạm)
PYTHONPATH=src .venv/bin/python -m pytest -q

# Kiểm tra cú pháp JavaScript
node --check app/static/js/app.js
```
