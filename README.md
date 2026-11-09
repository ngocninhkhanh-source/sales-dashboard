# 📊 Dashboard Hiệu suất Sale (Streamlit)

| Chỉ số | Nguồn |
|---|---|
| Lead mới, Deal mới, Deal thắng | Bitrix24 (inbound webhook) |
| Lead tồn theo stage (New, Call 1, Call 2, Call 3, Qualify, UnQLF) | Bitrix24, số lead hiện đang ở mỗi stage |
| Doanh thu = tổng giá trị (OPPORTUNITY) deal Won có ngày đóng trong kỳ | Bitrix24 |
| Số cuộc gọi, thời lượng gọi | VoiceCloud API |
| Số meeting có khách | Google Calendar |
| Meeting đã recap hay chưa | Gmail: dò Thư đã gửi tới khách của từng meeting |

Chưa có cấu hình thì app chạy **chế độ DEMO** với dữ liệu giả.

```
sales-dashboard/
├── app.py                          # toàn bộ ứng dụng
├── requirements.txt
├── .gitignore                      # chặn đẩy secrets lên GitHub
└── .streamlit/
    ├── config.toml                 # theme
    └── secrets.toml.example        # mẫu cấu hình (danh sách Sale + khoá API)
```

## Cách xác định recap

Mỗi meeting trong Google Calendar (có giờ cụ thể, có khách ngoài `internal_domains`, Sale không từ chối) được gán một trạng thái:

| Trạng thái | Điều kiện |
|---|---|
| ✅ Đã recap | Trong **Thư đã gửi** của Sale có email gửi (To/Cc/Bcc) tới ít nhất 1 khách của meeting, gửi từ lúc meeting bắt đầu đến `recap_window_hours` giờ sau khi kết thúc. Nếu khai báo `subject_keywords`, tiêu đề phải chứa một trong các từ đó. |
| ⏳ Chờ recap | Meeting đã kết thúc, chưa có email, vẫn còn trong hạn |
| ❌ Thiếu recap | Đã quá hạn mà không có email |
| 🗓️ Chưa diễn ra | Meeting chưa kết thúc |

**Tỷ lệ recap** = Đã recap / (Đã recap + Chờ + Thiếu).

Gmail không cho chia sẻ hộp thư, nên phần recap **bắt buộc Google Workspace + Domain-wide delegation**, với scope `gmail.readonly` và `calendar.readonly`. App chỉ dùng bộ lọc tìm kiếm (người nhận, thời gian, tiêu đề) và không đọc nội dung email.

## Chạy local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # điền thông tin thật
streamlit run app.py
```

## Deploy

1. Đưa code lên một repo GitHub **private**.
2. Vào [share.streamlit.io](https://share.streamlit.io) → **Create app** → chọn repo, nhánh `main`, file `app.py`.
3. Mở **Advanced settings**, dán nội dung `secrets.toml` vào ô **Secrets**, rồi bấm **Deploy**.

Hướng dẫn chi tiết từng bước có trong tài liệu "Hướng dẫn tự triển khai Dashboard".
