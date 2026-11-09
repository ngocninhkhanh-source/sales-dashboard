"""
Dashboard Hiệu suất Sale
========================
Nguồn dữ liệu:
  • Bitrix24        : Lead mới, Lead tồn theo stage, Deal mới, Deal thắng, Doanh thu (tổng giá trị deal thắng)
  • VoiceCloud      : số cuộc gọi & thời lượng gọi
  • Google Calendar : số Meeting với khách
  • Gmail           : với TỪNG meeting, kiểm tra Sale đã gửi email recap cho khách chưa + Estimate doanh thu từ báo giá

Google có 2 cách kết nối:
  A. CHẾ ĐỘ LEADER (khuyên dùng, không cần IT): Sale CC leader vào lời mời meeting và email recap
     → app chỉ đọc lịch + hộp thư của leader. Leader đăng nhập Google 1 lần ở trang "🔑 Kết nối Google".
  B. Service account + Domain-wide delegation (cần Super Admin Google Workspace).

Thông tin bí mật đặt trong `.streamlit/secrets.toml` (chạy local) hoặc ô "Secrets"
trên Streamlit Community Cloud — KHÔNG commit lên GitHub.
Chưa có secrets → app chạy CHẾ ĐỘ DEMO với dữ liệu giả.
"""

from __future__ import annotations

import hmac
import re
import time as _time
from urllib.parse import urlencode
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import plotly.express as px
import requests
import streamlit as st

# ════════════════════════════════════════════════════════════════════════════
# CẤU HÌNH CHUNG
# ════════════════════════════════════════════════════════════════════════════
TZ = ZoneInfo("Asia/Ho_Chi_Minh")
CACHE_TTL = 600  # giây — cache dữ liệu API 10 phút để tránh vượt giới hạn gọi API

COLOR_CALLS = "#2a78d6"
COLOR_MEETINGS = "#eb6834"

# Trạng thái recap của một meeting → màu trạng thái (luôn đi kèm biểu tượng + chữ)
ST_DONE, ST_WAIT, ST_MISS, ST_UPCOMING, ST_UNKNOWN = (
    "✅ Đã recap", "⏳ Chờ recap", "❌ Thiếu recap", "🗓️ Chưa diễn ra", "— Chưa kiểm tra",
)
STATUS_COLORS = {
    ST_DONE: "#0ca30c", ST_WAIT: "#fab219", ST_MISS: "#d03b3b",
    ST_UPCOMING: "#b4b2a9", ST_UNKNOWN: "#d9d8d2",
}

st.set_page_config(page_title="Dashboard Hiệu suất Sale", page_icon="📊", layout="wide")


# ════════════════════════════════════════════════════════════════════════════
# TIỆN ÍCH
# ════════════════════════════════════════════════════════════════════════════
def secret(section: str, key: str | None = None, default=None):
    """Đọc st.secrets an toàn — không lỗi khi chưa có file secrets."""
    try:
        if section not in st.secrets:
            return default
        node = st.secrets[section]
        return node if key is None else node.get(key, default)
    except Exception:
        return default


def configured(section: str) -> bool:
    return secret(section) is not None


def fmt_int(x) -> str:
    return f"{int(x or 0):,}".replace(",", ".")


def fmt_vnd(x: float) -> str:
    """Định dạng gọn: 1,25 tỷ / 350,4 tr / 12.500 ₫."""
    x = float(x or 0)
    swap = lambda t: t.replace(",", "X").replace(".", ",").replace("X", ".")
    if abs(x) >= 1e9:
        return swap(f"{x / 1e9:,.2f}") + " tỷ"
    if abs(x) >= 1e6:
        return swap(f"{x / 1e6:,.1f}") + " tr"
    return f"{x:,.0f} ₫".replace(",", ".")


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def period_range(mode: str, anchor: date, custom_range: tuple[date, date] | None = None) -> tuple[date, date]:
    """Tính toán khoảng ngày bắt đầu và kết thúc dựa trên chế độ lọc."""
    if mode == "Tùy chọn khoảng ngày":
        if custom_range and len(custom_range) == 2:
            return custom_range[0], custom_range[1]
        elif custom_range and len(custom_range) == 1:
            return custom_range[0], custom_range[0]
        return anchor, anchor

    if mode == "Hôm nay":
        return anchor, anchor
    if mode == "Hôm qua":
        prev = anchor - timedelta(days=1)
        return prev, prev
    if mode == "Tuần này":
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    if mode == "Tháng này":
        start = anchor.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        return start, end
    if mode == "Quý này":
        q_month = 3 * ((anchor.month - 1) // 3) + 1
        start = anchor.replace(month=q_month, day=1)
        if q_month + 3 > 12:
            end = date(anchor.year, 12, 31)
        else:
            end = date(anchor.year, q_month + 3, 1) - timedelta(days=1)
        return start, end

    return anchor, anchor


def to_dt_range(d_start: date, d_end: date) -> tuple[datetime, datetime]:
    """[00:00 ngày đầu, 00:00 ngày sau ngày cuối) theo giờ Việt Nam."""
    return (datetime.combine(d_start, time.min, TZ),
            datetime.combine(d_end + timedelta(days=1), time.min, TZ))


def clean_id(v) -> str:
    s = str(v if v is not None else "").strip()
    return s[:-2] if s.endswith(".0") else s


# ════════════════════════════════════════════════════════════════════════════
# MẬT KHẨU (tuỳ chọn: app_password trong secrets)
# ════════════════════════════════════════════════════════════════════════════
def check_password() -> bool:
    try:
        pw = st.secrets.get("app_password")
    except Exception:
        pw = None
    if not pw or st.session_state.get("auth_ok"):
        return True
    st.title("🔒 Dashboard Hiệu suất Sale")
    with st.form("login"):
        entered = st.text_input("Mật khẩu truy cập", type="password")
        if st.form_submit_button("Đăng nhập", type="primary"):
            if hmac.compare_digest(entered.encode(), str(pw).encode()):
                st.session_state.auth_ok = True
                st.rerun()
            st.error("Sai mật khẩu.")
    return False


if not check_password():
    st.stop()


# ════════════════════════════════════════════════════════════════════════════
# ĐĂNG NHẬP GOOGLE CỦA LEADER (OAuth)
# ════════════════════════════════════════════════════════════════════════════
OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def leader_mode() -> bool:
    return bool(secret("google_oauth", "refresh_token"))


def oauth_login_url() -> str:
    c = secret("google_oauth")
    params = {"client_id": c["client_id"], "redirect_uri": c.get("redirect_uri", "http://localhost"),
              "response_type": "code", "scope": " ".join(OAUTH_SCOPES),
              "access_type": "offline", "prompt": "select_account consent"}
    if c.get("leader_email"):
        params["login_hint"] = c["leader_email"]
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)


def oauth_exchange(code: str) -> dict:
    c = secret("google_oauth")
    tok = requests.post(TOKEN_URI, timeout=30, data={
        "code": code, "client_id": c["client_id"], "client_secret": c["client_secret"],
        "redirect_uri": c.get("redirect_uri", "http://localhost"), "grant_type": "authorization_code"}).json()
    if "access_token" in tok:
        prof = requests.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", timeout=30,
                            headers={"Authorization": f"Bearer {tok['access_token']}"}).json()
        tok["email"] = prof.get("emailAddress", "?")
    return tok


def extract_code(text: str) -> str:
    from urllib.parse import parse_qs, urlparse
    text = text.strip()
    if "code=" in text:
        return parse_qs(urlparse(text).query or text.split("?", 1)[-1]).get("code", [""])[0]
    return text


# ════════════════════════════════════════════════════════════════════════════
# DANH SÁCH SALE
# ════════════════════════════════════════════════════════════════════════════
ROSTER_COLS = ["sale", "email", "bitrix_id", "vc_id"]


def load_roster_from_secrets() -> pd.DataFrame:
    rows = [dict(r) for r in (secret("sales") or [])]
    df = pd.DataFrame({
        "sale": [str(r.get("name", "")).strip() for r in rows],
        "email": [str(r.get("email", "")).strip().lower() for r in rows],
        "bitrix_id": [clean_id(r.get("bitrix_id", "")) for r in rows],
        "vc_id": [clean_id(r.get("voicecloud_id", "")) for r in rows],
    }, columns=ROSTER_COLS)
    return df[df["sale"] != ""].drop_duplicates("sale").reset_index(drop=True)


@st.cache_data(ttl=CACHE_TTL, show_spinner="Đang tải danh sách Sale từ Google Sheets…")
def load_roster_from_sheet() -> pd.DataFrame:
    import gspread

    gc = gspread.service_account_from_dict(dict(secret("gcp_service_account")))
    ws = gc.open_by_key(secret("google_sheets", "spreadsheet_id")).worksheet(
        secret("google_sheets", "roster_tab", "Sales"))
    rows = ws.get_values(value_render_option="UNFORMATTED_VALUE")
    if len(rows) < 2:
        return pd.DataFrame(columns=ROSTER_COLS)
    df = pd.DataFrame(rows[1:], columns=[str(h).strip() for h in rows[0]])
    get = lambda c: df[c] if c in df.columns else pd.Series([""] * len(df))
    out = pd.DataFrame({
        "sale": get("Tên Sale").astype(str).str.strip(),
        "email": get("Email").astype(str).str.strip().str.lower(),
        "bitrix_id": get("Bitrix ID").map(clean_id),
        "vc_id": get("VoiceCloud ID").map(clean_id),
    })
    return out[out["sale"] != ""].drop_duplicates("sale").reset_index(drop=True)


# ════════════════════════════════════════════════════════════════════════════
# NGUỒN 1 — BITRIX24
# ════════════════════════════════════════════════════════════════════════════
def _bitrix(method: str, params: dict) -> dict:
    base = secret("bitrix24", "webhook_url").rstrip("/")
    r = requests.post(f"{base}/{method}.json", json=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"Bitrix24 {method}: {data.get('error_description', data['error'])}")
    return data


def _bitrix_list(method: str, params: dict, max_items: int = 20000) -> list[dict]:
    items, start = [], 0
    while True:
        data = _bitrix(method, {**params, "start": start})
        items.extend(data.get("result", []))
        if "next" not in data or len(items) >= max_items:
            return items
        start = data["next"]
        _time.sleep(0.5)


def _bitrix_batch(cmds: dict[str, str]) -> dict[str, int]:
    out, items = {}, list(cmds.items())
    for i in range(0, len(items), 50):
        chunk = dict(items[i:i + 50])
        res = _bitrix("batch", {"halt": 0, "cmd": chunk}).get("result", {})
        errs = res.get("result_error") or {}
        if isinstance(errs, dict) and errs:
            k, e = next(iter(errs.items()))
            raise RuntimeError(f"Bitrix24 batch ({k}): {e.get('error_description', e) if isinstance(e, dict) else e}")
        totals = res.get("result_total") or {}
        rows = res.get("result") or {}
        for k in chunk:
            t = totals.get(k) if isinstance(totals, dict) else None
            out[k] = int(t) if t is not None else len((rows.get(k) if isinstance(rows, dict) else None) or [])
        _time.sleep(0.5)
    return out


LEAD_STAGES_DEFAULT = ["New", "Call 1", "Call 2", "Call 3", "Qualify", "UnQLF"]


def lead_stage_config() -> tuple[list[str], list[str]]:
    stages = list(secret("bitrix24", "lead_stages", LEAD_STAGES_DEFAULT))
    closed = list(secret("bitrix24", "lead_closed_stages", ["UnQLF"]))
    return stages, closed


def _bitrix_frame(rows: list[dict], date_field: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["ts", "bitrix_id", "amount"])
    df = pd.DataFrame(rows)
    amount = pd.to_numeric(df["OPPORTUNITY"], errors="coerce") if "OPPORTUNITY" in df else 0
    return pd.DataFrame({
        "ts": pd.to_datetime(df[date_field], errors="coerce", utc=True).dt.tz_convert(TZ),
        "bitrix_id": df["ASSIGNED_BY_ID"].map(clean_id),
        "amount": pd.Series(amount, index=df.index).fillna(0),
    })


@st.cache_data(ttl=CACHE_TTL, show_spinner="Đang tải Lead & Deal từ Bitrix24…")
def load_bitrix(bitrix_ids: tuple[str, ...], d_start: date, d_end: date) -> dict[str, pd.DataFrame]:
    t0, t1 = (t.isoformat() for t in to_dt_range(d_start, d_end))
    deal_extra = {"CATEGORY_ID": c} if (c := secret("bitrix24", "deal_category_id")) is not None else {}
    order = {"ID": "ASC"}

    leads_new = _bitrix_list("crm.lead.list", {
        "filter": {">=DATE_CREATE": t0, "<DATE_CREATE": t1},
        "select": ["ID", "ASSIGNED_BY_ID", "DATE_CREATE"], "order": order})
    deals_new = _bitrix_list("crm.deal.list", {
        "filter": {">=DATE_CREATE": t0, "<DATE_CREATE": t1, **deal_extra},
        "select": ["ID", "ASSIGNED_BY_ID", "DATE_CREATE"], "order": order})
    deals_won = _bitrix_list("crm.deal.list", {
        "filter": {"STAGE_SEMANTIC_ID": "S", ">=CLOSEDATE": t0, "<CLOSEDATE": t1, **deal_extra},
        "select": ["ID", "ASSIGNED_BY_ID", "CLOSEDATE", "OPPORTUNITY", "CURRENCY_ID"], "order": order})

    stages, _ = lead_stage_config()