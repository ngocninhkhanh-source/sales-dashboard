import datetime
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st

# ==========================================
# 1. CẤU HÌNH TRANG STREAMLIT
# ==========================================
st.set_page_config(
    page_title="Sales Performance & Revenue Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("📊 Realtime Sales Performance & Revenue Dashboard")
st.markdown("Hệ thống theo dõi tự động Doanh thu, Lead, Cuộc gọi và Meetings")


# ==========================================
# 2. HÀM TÍCH HỢP API & DỮ LIỆU (FETCH DATA)
# ==========================================
@st.cache_data(ttl=300)  # Cache 5 phút để tránh quá tải API
def load_google_sheet_data():
    """Lấy danh sách Sales & Doanh thu từ Google Sheet."""
    # Lưu ý: Khi deploy, kết nối qua Google Sheets Service Account / gspread
    # Dưới đây là cấu hình dữ liệu mẫu nếu chưa cấu hình API Secret:
    try:
        # Code thực tế với gspread:
        # gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
        # sh = gc.open_by_key(st.secrets["sheet_id"])
        # worksheet = sh.worksheet("Revenue")
        # return pd.DataFrame(worksheet.get_all_records())
        pass
    except Exception:
        pass

    # Mockup Data để ứng dụng chạy ngay lập tức
    return pd.DataFrame(
        {
            "Sales_Name": [
                "Nguyễn Văn A",
                "Trần Thị B",
                "Lê Văn C",
                "Phạm Thị D",
            ],
            "Doanh_Thu": [120000000, 95000000, 150000000, 60000000],
            "Date": [
                datetime.date.today() - datetime.timedelta(days=i)
                for i in range(4)
            ],
        }
    )


@st.cache_data(ttl=300)
def fetch_bitrix_data():
    """Lấy tổng Deal & Lead tồn từ Bitrix24 Rest API."""
    try:
        # webhook_url = st.secrets["BITRIX_WEBHOOK_URL"]
        # res = requests.get(f"{webhook_url}/crm.lead.list?filter[STATUS_ID]=NEW").json()
        # lead_count = res.get('total', 0)
        pass
    except Exception:
        pass

    return {
        "lead_ton": 45,
        "deals_by_sales": {
            "Nguyễn Văn A": 12,
            "Trần Thị B": 8,
            "Lê Văn C": 15,
            "Phạm Thị D": 5,
        },
    }


@st.cache_data(ttl=300)
def fetch_voicecloud_data():
    """Lấy logs cuộc gọi từ portal.voicecloud.vn API."""
    try:
        # api_key = st.secrets["VOICECLOUD_API_KEY"]
        # headers = {"Authorization": f"Bearer {api_key}"}
        # res = requests.get("https://portal.voicecloud.vn/api/v1/call_logs", headers=headers).json()
        pass
    except Exception:
        pass

    return pd.DataFrame(
        {
            "Sales_Name": [
                "Nguyễn Văn A",
                "Trần Thị B",
                "Lê Văn C",
                "Phạm Thị D",
            ],
            "Cuoc_Goi": [45, 60, 38, 52],
            "Thoi_Luong_Phut": [120, 150, 95, 110],
        }
    )


@st.cache_data(ttl=300)
def fetch_calendar_email_data():
    """Lấy số Meeting & Recap từ Google Calendar / Gmail."""
    return pd.DataFrame(
        {
            "Sales_Name": [
                "Nguyễn Văn A",
                "Trần Thị B",
                "Lê Văn C",
                "Phạm Thị D",
            ],
            "Meeting": [5, 3, 7, 2],
            "Recap": [5, 3, 6, 2],
        }
    )


# ==========================================
# 3. THANH ĐIỀU HƯỚNG (SIDEBAR FILTERS)
# ==========================================
st.sidebar.header("🔍 Bộ Lọc Dữ Liệu")

time_filter = st.sidebar.selectbox(
    "Chọn mốc thời gian",
    options=["Hôm nay", "Tuần này", "Tháng này"],
    index=2,
)

if st.sidebar.button("🔄 Làm mới dữ liệu (Realtime)"):
    st.cache_data.clear()
    st.sidebar.success("Đã cập nhật dữ liệu mới nhất!")

# ==========================================
# 4. THỦ THUẬT XỬ LÝ DỮ LIỆU TỔNG HỢP
# ==========================================
df_sheets = load_google_sheet_data()
bitrix_info = fetch_bitrix_data()
df_calls = fetch_voicecloud_data()
df_activities = fetch_calendar_email_data()

# Merge toàn bộ dữ liệu theo Sales_Name
df_master = df_sheets.groupby("Sales_Name", as_index=False)["Doanh_Thu"].sum()
df_master["Deal_Dang_Xu_Ly"] = df_master["Sales_Name"].map(
    bitrix_info["deals_by_sales"]
)
df_master = df_master.merge(df_calls, on="Sales_Name", how="left")
df_master = df_master.merge(df_activities, on="Sales_Name", how="left")

# ==========================================
# 5. HIỂN THỊ METRICS TỔNG QUAN (KPI CARDS)
# ==========================================
col1, col2, col3, col4, col5 = st.columns(5)

total_revenue = df_master["Doanh_Thu"].sum()
total_deals = df_master["Deal_Dang_Xu_Ly"].sum()
lead_ton = bitrix_info["lead_ton"]
total_calls = df_master["Cuoc_Goi"].sum()
total_meetings = df_master["Meeting"].sum()

col1.metric("Tổng Doanh Thu", f"{total_revenue:,.0f} VNĐ")
col2.metric("Tổng Deal Đang Xử Lý", f"{total_deals}")
col3.metric("Lead Tồn (Bitrix24)", f"{lead_ton}", delta="-5 so với tuần trước")
col4.metric("Tổng Cuộc Gọi", f"{total_calls}")
col5.metric("Tổng Meetings", f"{total_meetings}")

st.markdown("---")

# ==========================================
# 6. BẢNG LEADERBOARD CHI TIẾT
# ==========================================
st.subheader("📋 Bảng Tổng Hợp Chi Tiết Theo Nhân Viên Sales")

# Format hiển thị cho đẹp mắt
df_display = df_master.copy()
df_display["Doanh_Thu"] = df_display["Doanh_Thu"].apply(
    lambda x: f"{x:,.0f} VNĐ"
)
df_display.columns = [
    "Tên Sales",
    "Doanh Thu",
    "Số Deal",
    "Số Cuộc Gọi",
    "Thời Lượng (Phút)",
    "Số Meeting",
    "Số Recap",
]

st.dataframe(df_display, use_container_width=True, hide_index=True)

# ==========================================
# 7. BIỂU ĐỒ TRỰC QUAN (CHARTS)
# ==========================================
c1, c2 = st.columns(2)

with c1:
    st.subheader("🏆 So Sánh Doanh Thu Giữa Các Sales")
    fig_rev = px.bar(
        df_master,
        x="Sales_Name",
        y="Doanh_Thu",
        text_auto=".2s",
        color="Doanh_Thu",
        color_continuous_scale="Blues",
        labels={"Sales_Name": "Sales", "Doanh_Thu": "Doanh Thu (VNĐ)"},
    )
    fig_rev.update_layout(showlegend=False)
    st.plotly_chart(fig_rev, use_container_width=True)

with c2:
    st.subheader("📞 Tương Quan Cuộc Gọi & Meetings")
    fig_activity = go.Figure(
        data=[
            go.Bar(
                name="Cuộc gọi",
                x=df_master["Sales_Name"],
                y=df_master["Cuoc_Goi"],
            ),
            go.Bar(
                name="Meetings",
                x=df_master["Sales_Name"],
                y=df_master["Meeting"],
            ),
        ]
    )
    fig_activity.update_layout(barmode="group")
    st.plotly_chart(fig_activity, use_container_width=True)