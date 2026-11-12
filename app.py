"""
Dashboard Hiệu suất Sale
========================
Nguồn dữ liệu:
  • Bitrix24        : Lead mới, Lead tồn theo stage, Deal mới, Deal won, Doanh thu (tổng giá trị deal won)
  • VoiceCloud      : số cuộc gọi & thời lượng gọi
  • Google Calendar : số Meeting với khách
  • Gmail           : kiểm tra email recap + Estimate doanh thu từ báo giá
  • Bitrix Sync     : Đồng bộ qua Extension / File JSON của Leader
"""

from __future__ import annotations

import base64
import hmac
import json
from html import escape
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
CACHE_TTL = 600  # giây — cache dữ liệu API 10 phút

COLOR_CALLS = "#2a78d6"
COLOR_MEETINGS = "#eb6834"

# Trạng thái recap
ST_DONE, ST_MISS, ST_UPCOMING, ST_UNKNOWN = (
    "✅ Đã recap", "❌ Thiếu recap", "🗓️ Chưa diễn ra", "— Chưa kiểm tra",
)
STATUS_COLORS = {
    ST_DONE: "#0ca30c", ST_MISS: "#d03b3b",
    ST_UPCOMING: "#b4b2a9", ST_UNKNOWN: "#d9d8d2",
}

APP_TITLE = "S2.4 Team Ngọc - Dashboard Hiệu suất Sale"
st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")


# ════════════════════════════════════════════════════════════════════════════
# TIỆN ÍCH
# ════════════════════════════════════════════════════════════════════════════
def secret(section: str, key: str | None = None, default=None):
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


def fmt_time_delta(td: timedelta | float) -> str:
    if pd.isna(td) or td is None:
        return "—"
    if isinstance(td, (int, float)):
        td = timedelta(seconds=td)
    
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "Gửi trước khi kết thúc"
    
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days} ngày")
    if hours > 0:
        parts.append(f"{hours} giờ")
    if minutes > 0 or not parts:
        parts.append(f"{minutes} phút")
        
    return " ".join(parts)


def period_range(mode: str, anchor: date, custom_range: tuple[date, date] | None = None) -> tuple[date, date]:
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
    if mode == "Tháng trước":
        end = anchor.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if mode == "Quý này":
        q_month = 3 * ((anchor.month - 1) // 3) + 1
        start = anchor.replace(month=q_month, day=1)
        if q_month + 3 > 12:
            end = date(anchor.year, 12, 31)
        else:
            end = date(anchor.year, q_month + 3, 1) - timedelta(days=1)
        return start, end

    return anchor, anchor


def period_label(d0: date, d1: date, today: date) -> str:
    """Tên ngắn của kỳ đang xem, vd "Tháng 11", "Quý 4", "10/11", "03/11 – 09/11"."""
    yr = lambda d: "" if d.year == today.year else f"/{d.year}"
    month_end = (d1.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    if d0.day == 1 and d1 == month_end and (d0.year, d0.month) == (d1.year, d1.month):
        return f"Tháng {d0.month}{yr(d0)}"
    if (d0.day == 1 and d0.month % 3 == 1 and d1 == month_end and d1.year == d0.year
            and d1.month == d0.month + 2):
        return f"Quý {(d0.month - 1) // 3 + 1}{yr(d0)}"
    if d0 == d1:
        return f"{d0:%d/%m}{yr(d0)}"
    return f"{d0:%d/%m} – {d1:%d/%m}{yr(d1)}"


def _period_badge(d0: date, d1: date, today: date) -> str:
    """Ảnh SVG chứa tên kỳ để hiện ở ô logo trên cùng thanh bên."""
    from html import escape
    t = escape(period_label(d0, d1, today))
    w = max(120, 15 * len(t) + 40)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="40" viewBox="0 0 {w} 40">'
            f'<rect x="0" y="2" width="{w}" height="36" rx="10" fill="rgb(62,180,137)"/>'
            f'<text x="{w / 2}" y="27" text-anchor="middle" font-family="Source Sans Pro, Arial, sans-serif" '
            f'font-size="22" font-weight="700" fill="white">📅 {t}</text></svg>')


def to_dt_range(d_start: date, d_end: date) -> tuple[datetime, datetime]:
    return (datetime.combine(d_start, time.min, TZ),
            datetime.combine(d_end + timedelta(days=1), time.min, TZ))


def clean_id(v) -> str:
    s = str(v if v is not None else "").strip()
    return s[:-2] if s.endswith(".0") else s


# ════════════════════════════════════════════════════════════════════════════
# MẬT KHẨU
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


def owner_mode() -> tuple[bool, bool]:
    """(là chủ dashboard?, đã cấu hình phân quyền?). Chủ = email trong [app].owner_emails (app riêng tư trên
    Streamlit Cloud) hoặc mở link có ?admin=<admin_key> / nhập admin_key ở thanh bên."""
    key = str(secret("app", "admin_key", "") or "")
    owners = [str(e).strip().lower() for e in (secret("app", "owner_emails", []) or []) if str(e).strip()]
    if not key and not owners:
        return True, False
    if st.session_state.get("is_owner"):
        return True, True
    try:
        em = str(getattr(st.user, "email", "") or "").lower()
    except Exception:
        em = ""
    if em and em in owners:
        st.session_state["is_owner"] = True
        return True, True
    q = st.query_params.get("admin", "")
    if key and q and hmac.compare_digest(str(q).encode(), key.encode()):
        st.session_state["is_owner"] = True
        return True, True
    return False, True


if not check_password():
    st.stop()


# ════════════════════════════════════════════════════════════════════════════
# ĐĂNG NHẬP GOOGLE LEADER (OAuth)
# ════════════════════════════════════════════════════════════════════════════
OAUTH_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/drive.file"]  # chỉ file do dashboard tự tạo (lưu dữ liệu Bitrix)
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
# NGUỒN 1 — BITRIX24 (Hỗ trợ đọc tự động từ Sync File / Google Drive)
# ════════════════════════════════════════════════════════════════════════════
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


@st.cache_data(ttl=60, show_spinner="Đang đọc dữ liệu Bitrix24 sync...")
def load_bitrix_from_sync_file():
    raw_data = None
    
    if "uploaded_bitrix_data" in st.session_state and st.session_state["uploaded_bitrix_data"]:
        raw_data = st.session_state["uploaded_bitrix_data"]

    if not raw_data and leader_mode():
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials
            c = secret("google_oauth")
            creds = Credentials(None, refresh_token=c["refresh_token"], client_id=c["client_id"],
                                client_secret=c["client_secret"], token_uri=TOKEN_URI, 
                                scopes=["https://www.googleapis.com/auth/drive.readonly"])
            drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)
            
            res = drive_svc.files().list(q="name = 'bitrix_sync_data.json' and trashed = false", 
                                         fields="files(id, name, modifiedTime)").execute()
            files = res.get("files", [])
            if files:
                content = drive_svc.files().get_media(fileId=files[0]["id"]).execute()
                raw_data = json.loads(content.decode("utf-8"))
        except Exception:
            pass

    if not raw_data:
        try:
            with open("bitrix_sync_data.json", "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception:
            return None

    if not raw_data:
        return None

    leads_raw = raw_data.get("leads_raw", [])
    deals_won_raw = raw_data.get("deals_won_raw", [])
    
    df_leads = _bitrix_frame(leads_raw, "DATE_CREATE") if leads_raw else pd.DataFrame(columns=["ts", "bitrix_id", "amount"])
    df_deals_won = _bitrix_frame(deals_won_raw, "CLOSEDATE") if deals_won_raw else pd.DataFrame(columns=["ts", "bitrix_id", "amount"])
    
    stages, _ = lead_stage_config()
    stage_counts = {}
    for l in leads_raw:
        uid = clean_id(l.get("ASSIGNED_BY_ID", ""))
        st_id = str(l.get("STATUS_ID", ""))
        stage_counts[(uid, st_id)] = stage_counts.get((uid, st_id), 0) + 1
        
    stage_rows = [{"bitrix_id": uid, "stage": st_name, "n": stage_counts.get((uid, st_name), 0)}
                  for uid in roster["bitrix_id"] for st_name in stages]
    df_stages = pd.DataFrame(stage_rows, columns=["bitrix_id", "stage", "n"])

    return {
        "leads_new": df_leads,
        "deals_new": pd.DataFrame(columns=["ts", "bitrix_id", "amount"]),
        "deals_won": df_deals_won,
        "lead_stages": df_stages,
        "updated_at": raw_data.get("updated_at", "Chưa rõ")
    }


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


LEAD_STAGES_DEFAULT = ["New", "Call 1", "Call 2", "Call 3", "Qualified", "Unqualified"]


LEAD_EXCLUDE_SOURCES_DEFAULT = ["Sale tự tìm", "Partner giới thiệu"]


def lead_exclude_sources() -> list[str]:
    """Nguồn lead KHÔNG tính vào bảng Lead tồn theo stage."""
    return list(secret("bitrix24", "lead_exclude_sources", LEAD_EXCLUDE_SOURCES_DEFAULT))


def lead_stage_config() -> tuple[list[str], list[str]]:
    stages = list(secret("bitrix24", "lead_stages", LEAD_STAGES_DEFAULT))
    closed = list(secret("bitrix24", "lead_closed_stages", ["Unqualified"]))
    return stages, closed


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
    statuses = _bitrix("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}}).get("result", [])
    by_name = {str(x.get("NAME", "")).strip().lower(): x["STATUS_ID"] for x in statuses}
    by_code = {str(x["STATUS_ID"]).lower(): x["STATUS_ID"] for x in statuses}
    codes, missing = {}, []
    for name in stages:
        code = by_name.get(name.strip().lower()) or by_code.get(name.strip().lower())
        if code:
            codes[name] = code
        else:
            missing.append(name)
    if missing:
        raise KeyError(f"Không thấy stage {missing} trong Bitrix24.")
    ids = [u for u in bitrix_ids if u]
    cmds = {
        f"u{uid}_{i}": "crm.lead.list?" + urlencode(
            {"filter[ASSIGNED_BY_ID]": uid, "filter[STATUS_ID]": codes[name], "select[0]": "ID"})
        for uid in ids for i, name in enumerate(stages)
    }
    totals = _bitrix_batch(cmds)
    stage_rows = [{"bitrix_id": uid, "stage": name, "n": totals[f"u{uid}_{i}"]}
                  for uid in ids for i, name in enumerate(stages)]

    return {
        "leads_new": _bitrix_frame(leads_new, "DATE_CREATE"),
        "deals_new": _bitrix_frame(deals_new, "DATE_CREATE"),
        "deals_won": _bitrix_frame(deals_won, "CLOSEDATE"),
        "lead_stages": pd.DataFrame(stage_rows, columns=["bitrix_id", "stage", "n"]),
    }


# ════════════════════════════════════════════════════════════════════════════
# NGUỒN 2 — GOOGLE CALENDAR + GMAIL
# ════════════════════════════════════════════════════════════════════════════
def _google_service(api: str, version: str, scopes: list[str], subject: str | None = None):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        dict(secret("gcp_service_account")), scopes=scopes)
    if subject:
        creds = creds.with_subject(subject)
    return build(api, version, credentials=creds, cache_discovery=False)


def _guests(ev: dict, owner: str, internal: list[str]) -> list[str]:
    out = []
    for a in ev.get("attendees", []):
        em = a.get("email", "").lower()
        if not em or em == owner or a.get("self") or a.get("resource") or em.endswith("calendar.google.com"):
            continue
        if internal and em.split("@")[-1] in internal:
            continue
        out.append(em)
    return out


def norm_text(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", str(t).lower().replace("đ", "d"))
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def tokens(t: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm_text(t))


DEAL_LINK_RE = re.compile(r"(?:/crm/deal/details/|deal\s*(?:id)?\s*[:#]\s*)(\d+)", re.I)


def deal_ref(ev: dict) -> str:
    """ID deal Bitrix24 mà sale dán vào mô tả / địa điểm lịch (link …/crm/deal/details/123/ hoặc 'Deal: 123')."""
    m = DEAL_LINK_RE.search(f"{ev.get('description', '')} {ev.get('location', '')}")
    return m.group(1) if m else ""


def _is_meeting(ev: dict, owner: str, guests: list[str], cfg: dict) -> bool:
    if ev.get("status") == "cancelled" or "dateTime" not in ev.get("start", {}):
        return False
    me = next((a for a in ev.get("attendees", []) if a.get("email", "").lower() == owner), None)
    if me and me.get("responseStatus") == "declined":
        return False
    kw = [norm_text(k) for k in cfg.get("title_keywords", []) if k]
    if kw:
        title = norm_text(ev.get("summary", ""))
        return any(k in title for k in kw)
    return len(ev.get("attendees", [])) >= int(cfg.get("min_attendees", 2)) and bool(guests)


def _leader_service(api: str, version: str):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    c = secret("google_oauth")
    creds = Credentials(None, refresh_token=c["refresh_token"], client_id=c["client_id"],
                        client_secret=c["client_secret"], token_uri=TOKEN_URI)  # giữ đúng quyền đã cấp
    return build(api, version, credentials=creds, cache_discovery=False)


# ── BITRIX24 QUA NÚT "📥 Cập nhật Bitrix" (bookmarklet, không cần webhook) ─────────
# Leader bấm dấu trang khi đang mở Bitrix24 → đọc CRM bằng phiên đăng nhập của leader → gửi bản
# tổng hợp theo ngày sang trang "📥 Cập nhật Bitrix" → lưu vào Google Drive của leader (drive.file).
SNAP_NAME = "sales-dashboard-bitrix.json"
# Mã JavaScript của nút (nhúng sẵn — không cần file riêng). App tự chèn cấu hình vào chỗ __CFG__.
BOOKMARKLET_SRC = r"""(async()=>{
const C=__CFG__;
if(!window.BX||!BX.bitrix_sessid){alert('Hãy bấm nút này khi đang mở một trang Bitrix24 (đã đăng nhập).');return;}
const box=document.createElement('div');
box.style.cssText='position:fixed;z-index:2147483647;top:16px;right:16px;width:380px;padding:16px;background:white;color:rgb(34,34,34);border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.25);font:14px/1.5 sans-serif;white-space:pre-line';
document.body.appendChild(box);
const say=t=>{box.innerText='📥 Cập nhật Bitrix → Dashboard\n'+t;};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const enc=(fd,k,v)=>{if(v!==null&&typeof v==='object'){for(const i in v)enc(fd,k+'['+i+']',v[i]);}else fd.append(k,v);};
const call=async(m,p)=>{for(let a=0;a<6;a++){const fd=new URLSearchParams();fd.append('sessid',BX.bitrix_sessid());for(const k in p)enc(fd,k,p[k]);const r=await fetch('/rest/'+m+'.json',{method:'POST',body:fd,credentials:'same-origin'});const d=await r.json();if(d.error==='QUERY_LIMIT_EXCEEDED'){await sleep(1500);continue;}if(d.error)throw new Error(m+': '+(d.error_description||d.error));return d;}throw new Error('Bitrix24 đang quá tải, thử lại sau ít phút.');};
const all=async(m,p,label)=>{let out=[],s=0;for(;;){const d=await call(m,Object.assign({},p,{start:s}));out=out.concat(d.result||[]);say('Đang đọc '+label+': '+out.length+(d.total?' / '+d.total:''));if(d.next===undefined)break;s=d.next;await sleep(300);}return out;};
const fast=async(m,p,label)=>{let out=[],last=0;for(;;){const f=Object.assign({},p.filter,{'>ID':last});const d=await call(m,Object.assign({},p,{filter:f,order:{ID:'ASC'},start:-1}));const r=d.result||[];out=out.concat(r);say('Đang đọc '+label+': '+out.length);if(r.length<50)break;last=r[r.length-1].ID;await sleep(250);}return out;};
try{
const now=new Date();const f=new Date(now.getFullYear(),now.getMonth()-C.months+1,1);
const from=f.getFullYear()+'-'+String(f.getMonth()+1).padStart(2,'0')+'-01';
const iso=from+'T00:00:00+07:00';
const pm=new Date(now.getFullYear(),now.getMonth()-1,1);const mFrom=pm.getFullYear()+'-'+String(pm.getMonth()+1).padStart(2,'0')+'-01';
const agg={},stages={};
const add=(u,day,i,v)=>{agg[u]=agg[u]||{};agg[u][day]=agg[u][day]||[0,0,0,0];agg[u][day][i]+=v;};
say('Đang đọc danh sách stage…');
const sts=(await call('crm.status.list',{filter:{ENTITY_ID:'STATUS'}})).result||[];
const code={};const missing=[];
for(const x of sts){code[String(x.NAME).trim()]=x.STATUS_ID;}for(const n of C.stages){if(!sts.some(x=>String(x.NAME).trim().toLowerCase()===n.toLowerCase()||String(x.STATUS_ID).toLowerCase()===n.toLowerCase())){missing.push(n);}}
const norm=t=>String(t||'').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[̀-ͯ]/g,'').split(/[^a-z0-9]+/).filter(Boolean).sort().join(' ');
const resolved=[],found={},unresolved=[];
for(const [bid,name,email] of C.users){let uid='',how='';
say('Đang tìm tài khoản Bitrix24 của '+name+'…');
if(email){const r=await call('user.get',{FILTER:{EMAIL:email}}).catch(()=>({}));const u=(r.result||[])[0];if(u){uid=String(u.ID);how='email';}}
if(!uid&&name){const r=await call('user.search',{FIND:name}).catch(()=>({}));const list=r.result||[];const n=norm(name);const u=list.find(x=>norm([x.LAST_NAME,x.SECOND_NAME,x.NAME].join(' '))===n)||(list.length===1?list[0]:null);if(u){uid=String(u.ID);how='tên';}}

if(uid){resolved.push([uid,name]);found[name]=[uid,how];}else{unresolved.push(name);}
await sleep(250);}
say('Đang đọc danh sách nguồn lead…');
const srcs=(await call('crm.status.list',{filter:{ENTITY_ID:'SOURCE'}}).catch(()=>({}))).result||[];
const excl=[],missingSrc=[];
for(const n of (C.excl||[])){const s=srcs.find(x=>norm(x.NAME)===norm(n)||String(x.STATUS_ID).toLowerCase()===String(n).toLowerCase());if(s){excl.push(s.STATUS_ID);}else{missingSrc.push(n);}}
say('Đang tìm trường lý do Unqualified…');
const lf=(await call('crm.lead.fields',{}).catch(()=>({}))).result||{};
const lab=k=>{const q=lf[k]||{};return String(q.listLabel||q.formLabel||q.title||k);};
const cands=Object.keys(lf).filter(k=>k.indexOf('UF_CRM')===0&&['enumeration','string','crm_status','list'].includes(lf[k].type));
const score=k=>{const t=new Set(norm(lab(k)).split(' '));return (t.has('ly')&&t.has('do')?2:0)+(['unqualified','unqualify','unqual','unqlf','unq','junk','reason'].some(w=>t.has(w))?2:0)+(t.has('khong')&&t.has('dat')?1:0)+(t.has('reason')?1:0);};
const df_=(await call('crm.deal.fields',{}).catch(()=>({}))).result||{};
const dlab=k=>{const q=df_[k]||{};return String(q.listLabel||q.formLabel||q.title||k);};
const pickU=n=>Object.keys(lf).find(k=>k.toLowerCase()===String(n).toLowerCase()||norm(lab(k))===norm(n))||'';
let rfs=[];const rMissing=[];
if(C.reason&&[].concat(C.reason).filter(Boolean).length){for(const n of [].concat(C.reason).filter(Boolean)){const k=pickU(n);if(k){rfs.push(k);}else{rMissing.push(n);}}}
else{rfs=cands.filter(k=>score(k)>=2).sort((a,b)=>score(b)-score(a)).slice(0,3);}
const nfs=[],nMissing=[];for(const n of [].concat(C.need||[]).filter(Boolean)){let k=pickU(n);if(!k){const z=norm(n);k=Object.keys(lf).find(q=>q.indexOf('UF_CRM')===0&&z&&norm(lab(q)).indexOf(z)>=0)||'';}if(k){if(!nfs.includes(k)&&!rfs.includes(k))nfs.push(k);}else{nMissing.push(n);}}
const nRe=new RegExp(C.needRe||'quan tâm\\s*:?\\s*([^.\\n;]+)','i');
const needOf=t=>{t=String(t||'').replace(/<br\s*\/?>/gi,'\n').replace(/<[^>]+>/g,' ').replace(/[ \t]+/g,' ').trim();if(!t)return '';const m=t.match(nRe);if(!m)return '(Khác / không theo mẫu)';const r=m[1].trim().slice(0,80);return r?r.charAt(0).toUpperCase()+r.slice(1):'';};
const rmaps={};
for(const k of rfs.concat(nfs)){const q=lf[k];const mp={};for(const it of (q.items||[])){mp[String(it.ID)]=String(it.VALUE).trim();}
if(q.type==='crm_status'&&q.statusType){const r=(await call('crm.status.list',{filter:{ENTITY_ID:q.statusType}}).catch(()=>({}))).result||[];for(const it of r){mp[String(it.STATUS_ID)]=String(it.NAME).trim();}}rmaps[k]=mp;}
const dateF=Object.keys(df_).filter(k=>k.indexOf('UF_CRM')===0&&['date','datetime'].includes(df_[k].type));
let rdf='';
rdf=Object.keys(df_).find(k=>C.revDate&&(k.toLowerCase()===String(C.revDate).toLowerCase()||norm(dlab(k))===norm(C.revDate)))||'';
if(!rdf){const rk=k=>{const t=new Set(norm(dlab(k)).split(' '));return t.has('doanh')&&(t.has('ghi')||t.has('nhan'))?(t.has('so')?2:(t.has('thu')?1:0)):0;};let best=0;for(const k of dateF){const sc=rk(k);if(sc>best){best=sc;rdf=k;}}}
const dcands=Object.keys(df_).filter(k=>k.indexOf('UF_CRM')===0&&['enumeration','string','crm_status','list'].includes(df_[k].type));
const dscore=k=>{const t=new Set(norm(dlab(k)).split(' '));return (t.has('ly')&&t.has('do')?2:0)+(['lost','fail','failed','thua','huy'].some(w=>t.has(w))||(t.has('that')&&t.has('bai'))?2:0);};
let lrf='';
if(C.lostReason){lrf=Object.keys(df_).find(k=>k.toLowerCase()===String(C.lostReason).toLowerCase()||norm(dlab(k))===norm(C.lostReason))||'';}
else{let best=0;for(const k of dcands){const sc=dscore(k);if(sc>best){best=sc;lrf=k;}}if(best<2)lrf='';}
const lmap={};
if(lrf){const q=df_[lrf];for(const it of (q.items||[])){lmap[String(it.ID)]=String(it.VALUE).trim();}
if(q.type==='crm_status'&&q.statusType){const r=(await call('crm.status.list',{filter:{ENTITY_ID:q.statusType}}).catch(()=>({}))).result||[];for(const it of r){lmap[String(it.STATUS_ID)]=String(it.NAME).trim();}}}
let pkf=Object.keys(df_).find(k=>C.pkg&&(k.toLowerCase()===String(C.pkg).toLowerCase()||norm(dlab(k))===norm(C.pkg)))||'';
if(!pkf){let best=0;for(const k of Object.keys(df_).filter(k=>k.indexOf('UF_CRM')===0)){const t=new Set(norm(dlab(k)).split(' '));const sc=(t.has('goi')?2:0)+(t.has('dich')&&t.has('vu')?2:0)+(t.has('package')?3:0);if(sc>best&&sc>=3){best=sc;pkf=k;}}}
const pkmap={};if(pkf){const q=df_[pkf];for(const it of (q.items||[])){pkmap[String(it.ID)]=String(it.VALUE).trim();}if(q.type==='crm_status'&&q.statusType){const r=(await call('crm.status.list',{filter:{ENTITY_ID:q.statusType}}).catch(()=>({}))).result||[];for(const it of r){pkmap[String(it.STATUS_ID)]=String(it.NAME).trim();}}}
const pkOf=x=>{if(!pkf)return '';const v=x[pkf];const arr=(Array.isArray(v)?v:[v]).filter(z=>z!==null&&z!==undefined&&z!==''&&z!==false);return arr.map(z=>pkmap[String(z)]||String(z)).join(', ');};
const dlost={},dwon={},warn=[],calls={},dopen={},dnew={},dlostList={};let callSrc='';
const allSt=(await call('crm.status.list',{}).catch(()=>({}))).result||[];
const dstages=allSt.filter(x=>String(x.ENTITY_ID).indexOf('DEAL_STAGE')===0).map(x=>[String(x.STATUS_ID),String(x.NAME).trim(),+x.SORT||0,String(x.ENTITY_ID),String(x.SEMANTICS||'')]);
const dcats={'0':'Pipeline chính'};try{for(const c of ((await call('crm.dealcategory.list',{}).catch(()=>({}))).result||[])){dcats[String(c.ID)]=String(c.NAME).trim();}}catch(e){}
const closedIds=sts.filter(x=>(C.closed||[]).some(n=>String(x.NAME).trim().toLowerCase()===n.toLowerCase()||String(x.STATUS_ID).toLowerCase()===n.toLowerCase())).map(x=>x.STATUS_ID);
const srcName={},stName={},want=[],ldet={};
for(const x of srcs){srcName[x.STATUS_ID]=String(x.NAME).trim();}
for(const x of sts){stName[x.STATUS_ID]=String(x.NAME).trim();}
for(const n of C.stages){const x=sts.find(x=>String(x.NAME).trim().toLowerCase()===n.toLowerCase()||String(x.STATUS_ID).toLowerCase()===n.toLowerCase());if(x){want.push(x.STATUS_ID);}}
for(const [u,name] of resolved){
const L=await all('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,'>=DATE_CREATE':iso},select:['ID','DATE_CREATE'],order:{ID:'ASC'}},'lead mới của '+name);
L.forEach(x=>add(name,String(x.DATE_CREATE).slice(0,10),0,1));
const D=await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,'>=DATE_CREATE':iso},select:['ID','DATE_CREATE','TITLE'],order:{ID:'ASC'}},'deal mới của '+name);
D.forEach(x=>add(name,String(x.DATE_CREATE).slice(0,10),1,1));dnew[name]=D.map(x=>[String(x.ID),String(x.TITLE||'').slice(0,100),String(x.DATE_CREATE||'')]);
const W=rdf?[]:await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'S','>=CLOSEDATE':iso},select:['ID','CLOSEDATE','OPPORTUNITY','TITLE','SOURCE_ID'].concat(pkf?[pkf]:[]),order:{ID:'ASC'}},'deal won của '+name);dwon[name]=[];const wl=(x,d)=>dwon[name].push([String(x.ID),String(x.TITLE||''),parseFloat(x.OPPORTUNITY)||0,x.SOURCE_ID?(srcName[x.SOURCE_ID]||String(x.SOURCE_ID)):'(Không có nguồn)',d,pkOf(x)]);
if(!rdf){W.forEach(x=>{const day=String(x.CLOSEDATE).slice(0,10);add(name,day,2,1);add(name,day,3,parseFloat(x.OPPORTUNITY)||0);wl(x,day);});}
if(rdf){const RV=await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'S',['>='+rdf]:from},select:['ID','OPPORTUNITY',rdf,'TITLE','SOURCE_ID'].concat(pkf?[pkf]:[]),order:{ID:'ASC'}},'doanh thu của '+name);RV.forEach(x=>{const d=String(x[rdf]||'').slice(0,10);if(d){add(name,d,2,1);add(name,d,3,parseFloat(x.OPPORTUNITY)||0);wl(x,d);}});}
stages[name]={};const m={};
if(want.length){let LS;try{LS=await fast('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,STATUS_ID:want,'>=DATE_CREATE':mFrom+'T00:00:00+07:00'},select:['ID','STATUS_ID','SOURCE_ID','DATE_CREATE'].concat(rfs).concat(nfs)},'lead theo stage của '+name);}catch(e){if(!rfs.length&&!nfs.length)throw e;warn.push('Không đọc được trường lý do Unqualified / nhu cầu: '+e.message);rfs=[];nfs.length=0;LS=await fast('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,STATUS_ID:want,'>=DATE_CREATE':mFrom+'T00:00:00+07:00'},select:['ID','STATUS_ID','SOURCE_ID','DATE_CREATE']},'lead theo stage của '+name);}
for(const x of LS){const sn=stName[x.STATUS_ID]||String(x.STATUS_ID);const so=x.SOURCE_ID?(srcName[x.SOURCE_ID]||String(x.SOURCE_ID)):'(Không có nguồn)';let rs='';if(rfs.length&&closedIds.includes(x.STATUS_ID)){rs=rfs.map(k=>{const v=x[k];const arr=(Array.isArray(v)?v:[v]).filter(z=>z!==null&&z!==undefined&&z!==''&&z!==false);return arr.map(z=>rmaps[k][String(z)]||String(z)).join(', ')||'(Chưa ghi lý do)';});}const nd=[...new Set(nfs.flatMap(k=>{const v=x[k];return (Array.isArray(v)?v:[v]).filter(z=>z!==null&&z!==undefined&&z!==''&&z!==false).map(z=>needOf(rmaps[k][String(z)]||String(z)));}).filter(Boolean))].join(', ');const k=JSON.stringify([sn,so,String(x.DATE_CREATE).slice(0,10),rs,nd]);m[k]=(m[k]||0)+1;if(!excl.includes(x.SOURCE_ID)){stages[name][sn]=(stages[name][sn]||0)+1;}}}
ldet[name]=Object.entries(m).map(([k,v])=>JSON.parse(k).concat([v]));
try{const DO=await fast('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'P'},select:['ID','TITLE','STAGE_ID','CATEGORY_ID','DATE_CREATE','DATE_MODIFY','OPPORTUNITY'].concat(pkf?[pkf]:[])},'deal tồn của '+name);dopen[name]=DO.map(x=>[String(x.ID),String(x.TITLE||'').slice(0,120),String(x.STAGE_ID),String(x.CATEGORY_ID||'0'),String(x.DATE_CREATE||''),parseFloat(x.OPPORTUNITY)||0,pkOf(x),String(x.DATE_MODIFY||'')]);}catch(e){dopen[name]=[];warn.push('Deal tồn của '+name+': '+e.message);}
if(C.calls!==false){calls[name]={};const ct=C.callTypes||[];const addc=(d,sec)=>{if(!d)return;const c=calls[name][d]||(calls[name][d]=[0,0]);c[0]++;c[1]+=sec;};
let got=false;if(callSrc!=='activity'){try{const CS=await all('voximplant.statistic.get',{FILTER:{PORTAL_USER_ID:u,'>=CALL_START_DATE':iso},SORT:'CALL_START_DATE',ORDER:'ASC'},'cuộc gọi của '+name);for(const x of CS){if(ct.length&&!ct.includes(+x.CALL_TYPE))continue;addc(String(x.CALL_START_DATE||'').slice(0,10),parseInt(x.CALL_DURATION)||0);}got=CS.length>0;if(got)callSrc='telephony';}catch(e){if(!callSrc)warn.push('Thống kê tổng đài: '+e.message);}}
if(!got&&callSrc!=='telephony'){try{const AS=await all('crm.activity.list',{filter:{TYPE_ID:2,RESPONSIBLE_ID:u,'>=START_TIME':iso},select:['ID','START_TIME','END_TIME','DIRECTION'],order:{ID:'ASC'}},'cuộc gọi (CRM) của '+name);for(const x of AS){if(ct.length&&!ct.includes(+x.DIRECTION===2?1:2))continue;const a=Date.parse(x.START_TIME),b=Date.parse(x.END_TIME);addc(String(x.START_TIME||'').slice(0,10),(a&&b&&b>a)?Math.round((b-a)/1000):0);}if(AS.length){callSrc='activity';}}catch(e){warn.push('Hoạt động gọi CRM: '+e.message);}}}
try{const LD=await fast('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'F','>=CLOSEDATE':mFrom+'T00:00:00+07:00'},select:['ID','SOURCE_ID','CLOSEDATE','TITLE','OPPORTUNITY'].concat(lrf?[lrf]:[])},'deal Lost của '+name);
const lm={};dlostList[name]=[];for(const x of LD){const so=x.SOURCE_ID?(srcName[x.SOURCE_ID]||String(x.SOURCE_ID)):'(Không có nguồn)';let rs='';if(lrf){const v=x[lrf];const arr=(Array.isArray(v)?v:[v]).filter(z=>z!==null&&z!==undefined&&z!==''&&z!==false);rs=arr.map(z=>lmap[String(z)]||String(z)).join(', ');}dlostList[name].push([String(x.ID),String(x.TITLE||'').slice(0,100),String(x.CLOSEDATE||'').slice(0,10),rs,parseFloat(x.OPPORTUNITY)||0]);const k=JSON.stringify([so,String(x.CLOSEDATE).slice(0,10),rs||'(Chưa ghi lý do)']);lm[k]=(lm[k]||0)+1;}
dlost[name]=Object.entries(lm).map(([k,v])=>JSON.parse(k).concat([v]));}catch(e){dlost[name]=[];warn.push('Deal Lost của '+name+': '+e.message);}
}
const dcm={};
if(C.comments!==false){try{
const uname={};for(const [uu,nn] of resolved)uname[uu]=nn;
const clean=t=>String(t||'').replace(/<br\s*\/?>/gi,'\n').replace(/\[\/?[A-Za-z*]+[^\]]*\]/g,'').replace(/<[^>]+>/g,' ').replace(/&nbsp;/g,' ').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&quot;/g,'"').replace(/&amp;/g,'&').replace(/[ \t]+/g,' ').replace(/ ?\n[\s]*/g,'\n').trim();
const ids=[];const seen={};const push=i=>{i=String(i);if(i&&!seen[i]){seen[i]=1;ids.push(i);}};
for(const n in dopen)dopen[n].slice().sort((a,b)=>String(b[7]).localeCompare(String(a[7]))).forEach(x=>push(x[0]));
for(const n in dwon)dwon[n].forEach(x=>push(x[0]));for(const n in dlostList)dlostList[n].forEach(x=>push(x[0]));
const lim=ids.slice(0,C.commentMax||1500);const qs=p=>{const q=new URLSearchParams();for(const k in p)enc(q,k,p[k]);return q.toString();};
const sel=['ID','CREATED','AUTHOR_ID','COMMENT'];
const nz=t=>String(t||'').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\s+/g,' ').trim();
const cmEx=(C.cmExcl||[]).map(nz).filter(Boolean);const cmSkip=t=>{const z=nz(t);return cmEx.some(e=>z.includes(e));};
for(let i=0;i<lim.length;i+=50){const part=lim.slice(i,i+50);const cmd={};
for(const id of part)cmd['c'+id]='crm.timeline.comment.list?'+qs({filter:{ENTITY_ID:id,ENTITY_TYPE:'deal'},select:sel,order:{ID:'DESC'}});
say('Đang đọc comment trên deal: '+Math.min(i+50,lim.length)+' / '+lim.length);
const d=await call('batch',{halt:0,cmd:cmd});const R=(d.result||{}).result||{},T=(d.result||{}).result_total||{};
for(const id of part){let a=R['c'+id]||[];const tot=+T['c'+id]||a.length;
if(tot>50&&a.length>1&&+a[0].ID<+a[a.length-1].ID){const r=await call('crm.timeline.comment.list',{filter:{ENTITY_ID:id,ENTITY_TYPE:'deal'},select:sel,start:Math.floor((tot-1)/50)*50}).catch(()=>({}));a=a.concat(r.result||[]);}
a.sort((x,y)=>+y.ID-+x.ID);const top=a.map(x=>[String(x.CREATED||''),String(x.AUTHOR_ID||''),clean(x.COMMENT).slice(0,C.commentLen||600)]).filter(x=>x[2]&&!cmSkip(x[2])).slice(0,C.commentN||5);
if(top.length)dcm[id]=top;}
await sleep(400);}
const unk=[...new Set(Object.values(dcm).flat().map(x=>x[1]).filter(x=>x&&!uname[x]))];
for(let i=0;i<unk.length;i+=50){const cmd={};for(const id of unk.slice(i,i+50))cmd['u'+id]='user.get?'+qs({ID:id});
try{const d=await call('batch',{halt:0,cmd:cmd});const R=(d.result||{}).result||{};for(const k in R){const u=(R[k]||[])[0];if(u)uname[String(u.ID)]=[u.LAST_NAME,u.SECOND_NAME,u.NAME].filter(Boolean).join(' ').trim();}}catch(e){}}
for(const id in dcm)dcm[id]=dcm[id].map(x=>[x[0],uname[x[1]]||(x[1]?'User '+x[1]:''),x[2]]);
if(ids.length>lim.length)warn.push('Chỉ đọc comment của '+lim.length+'/'+ids.length+' deal (deal cập nhật gần nhất trước).');
}catch(e){warn.push('Không đọc được comment trên deal: '+e.message);}}
const data={v:15,dcm:dcm,dlostList:dlostList,dnew:dnew,dopen:dopen,dstages:dstages,dcats:dcats,calls:calls,callSrc:callSrc,dwon:dwon,pkgField:pkf?[pkf,dlab(pkf)]:null,bv:C.bv,warn:warn,ldet:ldet,ldetFrom:mFrom,reasonField:rfs.length?[rfs[0],lab(rfs[0])]:null,reasonFields:rfs.map(k=>[k,lab(k)]),needFields:nfs.map(k=>[k,lab(k)]),needMissing:nMissing,reasonMissing:rMissing,revField:rdf?[rdf,dlab(rdf)]:null,revCands:dateF.map(dlab).slice(0,40),reasonCands:cands.map(lab).slice(0,40),dlost:dlost,lostField:lrf?[lrf,dlab(lrf)]:null,lostCands:dcands.map(dlab).slice(0,40),users:found,unresolved:unresolved,exclSources:C.excl||[],missingSources:missingSrc,allSources:srcs.map(x=>x.NAME),at:Date.now(),from:from,portal:location.host,agg:agg,stages:stages,missing:missing,allStages:sts.map(x=>x.NAME)};
const b64u=buf=>{let t='';const u8=new Uint8Array(buf);for(let i=0;i<u8.length;i+=8192){t+=String.fromCharCode.apply(null,u8.subarray(i,i+8192));}return btoa(t).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');};
const raw=new TextEncoder().encode(JSON.stringify(data));
let b64;try{const cs=new CompressionStream('deflate-raw');const wr=cs.writable.getWriter();wr.write(raw);wr.close();b64='z'+b64u(await new Response(cs.readable).arrayBuffer());}catch(e){b64=b64u(raw);}
const viaUrl=(C.url+'?bx='+b64).length<6000;
say('✅ Đã đọc xong dữ liệu Bitrix24 từ '+from.split('-').reverse().join('/')+' (nút phiên bản '+C.bv+').\n'+(warn.length?'⚠️ '+warn.join('\n⚠️ ')+'\n':'')+(viaUrl?'Bấm nút dưới để gửi sang dashboard.':'Dữ liệu lớn: bấm nút dưới — dữ liệu được copy sẵn, trang dashboard mở ra thì bấm vào ô dán (Cmd/Ctrl + V) rồi bấm Lưu.'));
const btn=document.createElement('button');btn.innerText='📤 Gửi sang dashboard';btn.style.cssText='margin-top:10px;padding:8px 14px;background:rgb(42,120,214);color:white;border:0;border-radius:6px;font-weight:600;cursor:pointer';
const ta=document.createElement('textarea');ta.value=b64;ta.readOnly=true;ta.style.cssText='display:block;width:340px;height:60px;margin-top:8px;font:11px monospace';
window.addEventListener('message',e=>{if(e.data==='bx-ready'&&e.source){try{e.source.postMessage({bx:b64},'*');}catch(er){}}else if(e.data==='bx-saved'){say('✅ Dashboard đã nhận dữ liệu và đang lưu. Có thể đóng khung này.');box.appendChild(x);setTimeout(()=>box.remove(),8000);}});
btn.onclick=async()=>{window.open(viaUrl?C.url+'?bx='+b64:C.url+'?paste=1','_blank');if(viaUrl){box.remove();return;}let ok=false;try{await navigator.clipboard.writeText(b64);ok=true;}catch(e){}say('📤 Đang gửi sang dashboard (tab mới)… thường tự xong sau vài giây.\nNếu dashboard không tự lưu: '+(ok?'bấm vào ô dán trên dashboard, nhấn Cmd/Ctrl + V rồi bấm Lưu.':'bấm vào ô dưới, nhấn Cmd/Ctrl + A rồi Cmd/Ctrl + C, sang dashboard dán vào ô và bấm Lưu.'));box.appendChild(ta);ta.select();box.appendChild(x);};
const x=document.createElement('button');x.innerText='Đóng';x.style.cssText='margin:10px 0 0 10px;padding:8px 14px;background:white;border:1px solid rgb(200,200,200);border-radius:6px;cursor:pointer';x.onclick=()=>box.remove();
box.appendChild(btn);box.appendChild(x);
}catch(e){say('❌ Lỗi: '+e.message);const x=document.createElement('button');x.innerText='Đóng';x.onclick=()=>box.remove();box.appendChild(x);}
})();"""

def bitrix_mode() -> str:
    return "webhook" if secret("bitrix24", "webhook_url") else "bookmarklet"

@st.cache_resource
def _mem_store() -> dict:
    return {}

@st.cache_data(ttl=300, show_spinner=False)
def _load_snapshot_drive(name: str = SNAP_NAME) -> dict | None:
    svc = _leader_service("drive", "v3")
    files = svc.files().list(q=f"name='{name}' and trashed=false", spaces="drive",
                             orderBy="modifiedTime desc", fields="files(id)").execute().get("files", [])
    if not files:
        return None
    return json.loads(svc.files().get_media(fileId=files[0]["id"]).execute())

def get_snapshot(name: str = SNAP_NAME) -> tuple[dict | None, str, str]:
    """(dữ liệu, nơi lưu, lỗi Drive nếu có) — lấy bản mới nhất giữa Google Drive và bộ nhớ tạm."""
    drive, err = None, ""
    if leader_mode():
        try:
            drive = _load_snapshot_drive(name)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    mem = _mem_store().get(name)
    if drive and (not mem or drive.get("at", 0) >= mem.get("at", 0)):
        return drive, "Google Drive", err
    return mem, ("bộ nhớ tạm" if mem else ""), err

def save_snapshot(snap: dict, name: str = SNAP_NAME) -> tuple[str, str]:
    """Lưu vào bộ nhớ tạm (luôn) + Google Drive của leader (nếu được). Trả về (nơi lưu, lỗi)."""
    _mem_store()[name] = snap
    if not leader_mode():
        return "bộ nhớ tạm", "Chưa kết nối Google của leader (trang 🔑 Kết nối Google)."
    try:
        from googleapiclient.http import MediaInMemoryUpload

        svc = _leader_service("drive", "v3")
        media = MediaInMemoryUpload(json.dumps(snap).encode(), mimetype="application/json")
        files = svc.files().list(q=f"name='{name}' and trashed=false", spaces="drive",
                                 fields="files(id)").execute().get("files", [])
        if files:
            svc.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            svc.files().create(body={"name": name, "mimeType": "application/json"},
                               media_body=media, fields="id").execute()
        _load_snapshot_drive.clear()
        return "Google Drive", ""
    except Exception as e:
        return "bộ nhớ tạm", f"{type(e).__name__}: {e}"

ADJ_NAME = "sales-dashboard-revenue-adjust.json"
ADJ_COLS = ["day", "sale", "amount", "deals", "source", "package", "note"]
ADJ_SOURCES = ["Sale tự tìm", "MKT", "Hotline/Chat", "Partner", "Up sell"]


def load_adjustments() -> pd.DataFrame:
    """Deal won thêm tay (ngoài Bitrix / bù lệch) — lưu trên Google Drive của leader, file riêng."""
    snap_, _, _ = get_snapshot(ADJ_NAME)
    st.session_state["_adj_raw"] = snap_ or {}
    df = pd.DataFrame((snap_ or {}).get("rows", []), columns=ADJ_COLS)
    df["day"] = pd.to_datetime(df["day"], errors="coerce").dt.date
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    df["deals"] = pd.to_numeric(df["deals"], errors="coerce").fillna(0).astype(int)
    for c in ("sale", "source", "package", "note"):
        df[c] = df[c].fillna("").astype(str)
    return df[df["day"].notna() & (df["sale"] != "")].reset_index(drop=True)


def load_overrides() -> dict:
    """{deal_id: {"amount": số tiền đúng, "note": ...}} — sửa số tiền deal won đọc từ Bitrix."""
    return {str(k): v for k, v in ((st.session_state.get("_adj_raw") or {}).get("overrides") or {}).items()
            if isinstance(v, dict) and v.get("amount") is not None}


def save_adjustments(rows: list | None = None, overrides: dict | None = None) -> tuple[str, str]:
    raw = dict(st.session_state.get("_adj_raw") or {})
    if rows is not None:
        raw["rows"] = rows
    if overrides is not None:
        raw["overrides"] = overrides
    raw["at"] = int(_time.time() * 1000)
    st.session_state["_adj_raw"] = raw
    return save_snapshot(raw, ADJ_NAME)


def decode_payload(raw: str, param: str = "bx") -> dict:
    """Giải mã dữ liệu nút bookmarklet gửi sang (base64url, có thể nén deflate với tiền tố 'z')."""
    raw = raw.strip()
    if f"{param}=" in raw:
        raw = raw.split(f"{param}=", 1)[1].split("&", 1)[0]
    raw = "".join(raw.split())
    if raw.startswith("z"):
        import zlib
        body = raw[1:] + "=" * (-len(raw[1:]) % 4)
        return json.loads(zlib.decompress(base64.urlsafe_b64decode(body.encode()), -15).decode("utf-8"))
    raw += "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(raw.encode()).decode("utf-8"))


def decode_snapshot(raw: str) -> dict:
    raw = raw.strip()
    if "bx=" in raw:
        raw = raw.split("bx=", 1)[1].split("&", 1)[0]
    raw = "".join(raw.split())
    if raw.startswith("z"):  # dữ liệu đã nén (deflate) bởi nút Cập nhật Bitrix
        import zlib
        body = raw[1:] + "=" * (-len(raw[1:]) % 4)
        snap = json.loads(zlib.decompress(base64.urlsafe_b64decode(body.encode()), -15).decode("utf-8"))
    else:
        raw += "=" * (-len(raw) % 4)
        snap = json.loads(base64.urlsafe_b64decode(raw.encode()).decode("utf-8"))
    if not isinstance(snap, dict) or "agg" not in snap or "stages" not in snap:
        raise ValueError("Dữ liệu không đúng định dạng của nút Cập nhật Bitrix.")
    return snap

def snapshot_frames(snap: dict, id2sale: dict, d_start: date, d_end: date) -> dict[str, pd.DataFrame]:
    """Bản tổng hợp theo ngày → các bảng giống chế độ webhook (mỗi lead/deal 1 dòng)."""
    leads, deals, won, revenue = [], [], [], []
    names = set(id2sale.values())
    key2sale = lambda k: k if k in names else id2sale.get(clean_id(k))
    for uid, days in snap.get("agg", {}).items():
        sale = key2sale(uid)
        if not sale:
            continue
        for day, (nl, nd, nw, rev) in days.items():
            d = date.fromisoformat(day)
            if not (d_start <= d <= d_end):
                continue
            ts = datetime.combine(d, time(12), TZ)
            leads += [{"ts": ts, "sale": sale}] * int(nl)
            deals += [{"ts": ts, "sale": sale}] * int(nd)
            won += [{"ts": ts, "sale": sale, "amount": float(rev) if i == 0 else 0.0} for i in range(int(nw))]
            if float(rev):
                revenue.append({"ts": ts, "sale": sale, "amount": float(rev)})
    call_rows = []
    for u, days in (snap.get("calls") or {}).items():
        sale = key2sale(u)
        if not sale:
            continue
        for day, (n, sec) in days.items():
            try:
                d = date.fromisoformat(day)
            except ValueError:
                continue
            if d_start <= d <= d_end and int(n):
                ts = datetime.combine(d, time(12), TZ)
                call_rows += [{"ts": ts, "sale": sale, "duration": float(sec) / int(n)}] * int(n)
    cfg = {x.strip().lower(): x for x in lead_stage_config()[0]}  # khớp tên stage không phân biệt hoa thường
    detail = pd.DataFrame(
        [{"sale": key2sale(u), "stage": cfg.get(str(r[0]).strip().lower(), r[0]), "source": r[1], "created": r[2],
          "reason": r[3] if len(r) >= 5 else "", "n": int(r[-1]), "need": r[4] if len(r) >= 6 else ""}
         for u, rows in (snap.get("ldet") or {}).items() if key2sale(u) for r in rows],
        columns=LEAD_DETAIL_COLS)
    portal = snap.get("portal") or ""
    won_list = pd.DataFrame(
        [{"sale": key2sale(u), "id": r[0], "title": r[1], "amount": float(r[2]), "source": r[3],
          "day": r[4], "link": f"https://{portal}/crm/deal/details/{r[0]}/" if portal else "",
          "package": (r[5] if len(r) > 5 else "") or "(Chưa chọn gói)"}
         for u, rows in (snap.get("dwon") or {}).items() if key2sale(u) for r in rows], columns=WON_LIST_COLS)
    won_list["day"] = pd.to_datetime(won_list["day"], errors="coerce").dt.date
    won_list = won_list[won_list["day"].map(lambda d: isinstance(d, date) and d_start <= d <= d_end).astype(bool)]
    deal_lost = pd.DataFrame(
        [{"sale": key2sale(u), "source": r[0], "closed": r[1], "reason": r[2], "n": int(r[3])}
         for u, rows in (snap.get("dlost") or {}).items() if key2sale(u) for r in rows], columns=DEAL_LOST_COLS)
    deal_lost["closed"] = pd.to_datetime(deal_lost["closed"], errors="coerce").dt.date
    if "ldet" in snap:
        detail["created"] = pd.to_datetime(detail["created"], errors="coerce").dt.date
        m0 = datetime.now(TZ).date().replace(day=1)
        stages = lead_stage_summary(detail[detail["created"].map(lambda d: isinstance(d, date) and d >= m0).astype(bool)])
    else:  # dữ liệu từ nút cũ: chỉ có số đếm theo stage
        stages = pd.DataFrame([{"sale": key2sale(u), "stage": cfg.get(str(k).strip().lower(), k), "n": int(n)}
                               for u, m in snap.get("stages", {}).items() if key2sale(u) for k, n in m.items()],
                              columns=["sale", "stage", "n"])
    return {"leads_new": pd.DataFrame(leads, columns=["ts", "sale"]),
            "deals_new": pd.DataFrame(deals, columns=["ts", "sale"]),
            "deals_won": pd.DataFrame(won, columns=["ts", "sale", "amount"]),
            "revenue": pd.DataFrame(revenue, columns=["ts", "sale", "amount"]),
            "lead_stages": stages,
            "lead_detail": detail,
            "deal_lost": deal_lost,
            "won_list": won_list,
            "deal_open": deal_open_frame(snap, key2sale),
            "new_deal_list": new_deal_list_frame(snap, key2sale, d_start, d_end),
            "deal_all": deal_all_frame(snap, key2sale),
            **({"calls": pd.DataFrame(call_rows, columns=["ts", "sale", "duration"])} if "calls" in snap else {})}


LEAD_DETAIL_COLS = ["sale", "stage", "source", "created", "reason", "n", "need"]
NEED_FIELDS_DEFAULT = ["Nội dung tư vấn"]
DEAL_LOST_COLS = ["sale", "source", "closed", "reason", "n"]
WON_LIST_COLS = ["sale", "id", "title", "amount", "source", "day", "link", "package"]
DEAL_OPEN_COLS = ["sale", "stage_id", "stage", "cat", "order", "created", "n", "amount", "id", "title", "created_ts", "link",
                  "package", "modified_ts", "comments"]


NEW_DEAL_COLS = ["sale", "ts", "title", "link"]
DEAL_ALL_COLS = ["id", "sale", "title", "status", "stage", "amount", "modified_ts", "closed", "lost_reason", "link",
                 "comments"]


COMMENT_EXCLUDE_DEFAULT = ["Đã reset trạng thái deal sau khi Copy deal"]


def comment_exclude() -> list[str]:
    """Comment tự động của hệ thống (chứa một trong các cụm này) không hiển thị."""
    return [str(x) for x in (secret("bitrix24", "comment_exclude", COMMENT_EXCLUDE_DEFAULT) or []) if str(x).strip()]


def _cm_skip(text: str) -> bool:
    z = " ".join(norm_text(text).split())
    return any(" ".join(norm_text(e).split()) in z for e in comment_exclude())


def _cm_rows(items) -> list[tuple[str, str, str]]:
    """[(thời gian 'dd/mm/yyyy HH:MM', người viết, nội dung)] — mới nhất trước, bỏ comment tự động."""
    out = []
    for it in items if isinstance(items, (list, tuple)) else []:
        created, author, text = (list(it) + ["", "", ""])[:3]
        if _cm_skip(text):
            continue
        ts = pd.to_datetime(created, errors="coerce", utc=True)
        out.append((ts.tz_convert(TZ).strftime("%d/%m/%Y %H:%M") if not pd.isna(ts) else "", str(author or ""),
                    str(text or "")))
    return out


def fmt_comments(items) -> str:
    """Bản chữ (cho file CSV): '09/11/2026 15:05 · Nhi: nội dung ⏐ …'."""
    return "  ⏐  ".join((" · ".join(x for x in (w, a) if x) + ": " if (w or a) else "") + t.replace("\n", " / ")
                        for w, a, t in _cm_rows(items))


def comment_cell(items, short: int = 48) -> str:
    """Ô HTML: comment mới nhất (rút gọn); rê chuột hiện popup toàn bộ lịch sử comment."""
    rows = _cm_rows(items)
    if not rows:
        return '<span class="cm0">—</span>'
    first = rows[0][2].replace("\n", " ")
    lab = escape(first[:short] + ("…" if len(first) > short else ""))
    lis = "".join(f'<div class="ci"><div class="ch">{escape(" · ".join(x for x in (a, w) if x))}</div>'
                  f'<div class="ct">{escape(t)}</div></div>' for w, a, t in rows)
    return (f'<span class="cm">{lab}</span><div class="cpop"><div class="cph">Lịch sử comment Sales · '
            f'{len(rows)} comment (mới nhất trên cùng)</div>{lis}</div>')


POP_TABLE_CSS = """<style>
.pt{border-collapse:collapse;width:100%;font-size:13.5px;font-family:inherit}
.pt th,.pt td{border-bottom:1px solid rgba(128,128,128,.22);padding:7px 9px;text-align:left;vertical-align:top}
.pt th{background:rgba(62,180,137,.16);font-weight:700;position:sticky;top:0;z-index:2}
.pt td.n{text-align:right;white-space:nowrap} .pt td.nw{white-space:nowrap}
.pt td.w5{min-width:90px} .pt td.w1{min-width:140px} .pt td.w2{min-width:105px} .pt td.w3{min-width:140px} .pt td.w4{min-width:85px}
.pt tr:hover td{background:rgba(62,180,137,.06)}
.pt a{color:rgb(28,131,225);text-decoration:none} .pt a:hover{text-decoration:underline}
.pt .mut{color:rgba(128,128,128,.85);font-size:12px}
.pt td.c{position:relative;min-width:150px;max-width:230px;cursor:help}
.pt .cm{border-bottom:1px dotted currentColor} .pt .cm0{color:rgba(128,128,128,.6)}
.pt .cpop{display:none;position:absolute;z-index:1000;right:0;top:100%;width:520px;max-height:380px;overflow-y:auto;
  background:#111827;color:#f3f4f6;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.35);padding:12px 16px;
  text-align:left;font-weight:400;cursor:auto}
.pt tr.up td.c .cpop{top:auto;bottom:100%}
.pt td.c:hover .cpop{display:block}
.pt .cph{color:#fcd34d;font-weight:700;padding-bottom:8px;margin-bottom:6px;border-bottom:1px solid rgba(255,255,255,.18)}
.pt .ci{padding:7px 0;border-bottom:1px solid rgba(255,255,255,.12)} .pt .ci:last-child{border-bottom:0}
.pt .ch{color:#fbbf24;font-weight:700;margin-bottom:2px} .pt .ct{white-space:pre-line;line-height:1.45}
</style>"""


def pop_table(header: list[str], rows: list[list[str]], classes: list[str]) -> str:
    """Bảng HTML có ô comment rê chuột hiện popup. rows: các ô đã escape; classes: lớp CSS từng cột."""
    head = "".join(f"<th>{escape(h)}</th>" for h in header)
    body = []
    for i, r in enumerate(rows):
        up = ' class="up"' if len(rows) > 6 and i >= len(rows) - 4 else ""
        body.append(f"<tr{up}>" + "".join(f'<td class="{c}">{v}</td>' for c, v in zip(classes, r)) + "</tr>")
    return f'{POP_TABLE_CSS}<table class="pt"><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def deal_all_frame(snap: dict, key2sale) -> pd.DataFrame:
    """Mọi deal đã đọc từ Bitrix (đang mở, won, lost) để nối meeting ↔ deal."""
    portal = snap.get("portal") or ""
    link = lambda i: f"https://{portal}/crm/deal/details/{i}/" if portal and i else ""
    rows = []
    op = deal_open_frame(snap, key2sale)
    for r in op[op["id"].astype(str) != ""].itertuples():
        rows.append({"id": str(r.id), "sale": r.sale, "title": r.title, "status": "open", "stage": r.stage,
                     "amount": r.amount, "modified_ts": r.modified_ts, "closed": None, "lost_reason": "",
                     "link": r.link})
    for u, items in (snap.get("dwon") or {}).items():
        if key2sale(u):
            for it in items:
                rows.append({"id": str(it[0]), "sale": key2sale(u), "title": it[1], "status": "won", "stage": "Won",
                             "amount": float(it[2]), "modified_ts": pd.NaT, "closed": it[4], "lost_reason": "",
                             "link": link(it[0])})
    for u, items in (snap.get("dlostList") or {}).items():
        if key2sale(u):
            for did, title, closed, reason, amt in items:
                rows.append({"id": str(did), "sale": key2sale(u), "title": title, "status": "lost", "stage": "Lost",
                             "amount": float(amt), "modified_ts": pd.NaT, "closed": closed,
                             "lost_reason": reason or "(Chưa ghi lý do)", "link": link(did)})
    df = pd.DataFrame(rows, columns=DEAL_ALL_COLS)
    cm = snap.get("dcm") or {}
    df["comments"] = df["id"].map(lambda i: cm.get(str(i)) or [])
    return df.drop_duplicates("id", keep="first")


def new_deal_list_frame(snap: dict, key2sale, d_start: date, d_end: date) -> pd.DataFrame:
    """Từng deal mới tạo (tên, giờ tạo, link) — để hiện popup khi rê chuột ở bảng theo ngày."""
    portal = snap.get("portal") or ""
    rows = [{"sale": key2sale(u), "ts": c, "title": t,
             "link": f"https://{portal}/crm/deal/details/{i}/" if portal else ""}
            for u, items in (snap.get("dnew") or {}).items() if key2sale(u) for i, t, c in items]
    df = pd.DataFrame(rows, columns=NEW_DEAL_COLS)
    ts = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df["ts"] = ts.dt.tz_convert(TZ) if len(df) else ts
    return df[df["ts"].map(lambda x: not pd.isna(x) and d_start <= x.date() <= d_end).astype(bool)]


def deal_open_frame(snap: dict, key2sale) -> pd.DataFrame:
    """Deal đang xử lý (chưa Won/Lost) theo sale × stage × ngày tạo, tên stage và thứ tự giống Bitrix24."""
    cats = snap.get("dcats") or {"0": "Pipeline chính"}
    info = {}
    for sid, name, sort, ent, *_ in snap.get("dstages") or []:
        cat = ent.split("DEAL_STAGE_", 1)[1] if ent.startswith("DEAL_STAGE_") else "0"
        info[sid] = (name, cat, int(sort))
    portal = snap.get("portal") or ""
    rows = []
    for u, items in (snap.get("dopen") or {}).items():
        sale = key2sale(u)
        if not sale:
            continue
        for it in items:
            pkg, modified = "", ""
            if len(it) >= 6:   # nút mới: mỗi deal 1 dòng
                did, title, sid, cat, created, amt = it[:6]
                pkg = it[6] if len(it) > 6 else ""
                modified = it[7] if len(it) > 7 else ""
                n = 1
            else:              # nút cũ: đã gộp theo ngày
                sid, cat, created, n, amt = it
                did, title = "", ""
            name, cat2, sort = info.get(sid, (sid, cat, 9999))
            rows.append({"sale": sale, "stage_id": sid, "stage": name, "cat": cats.get(str(cat2), f"Pipeline {cat2}"),
                         "order": sort, "created": str(created)[:10], "n": int(n), "amount": float(amt),
                         "id": did, "title": title, "created_ts": created,
                         "link": f"https://{portal}/crm/deal/details/{did}/" if portal and did else "",
                         "package": pkg, "modified_ts": modified})
    df = pd.DataFrame(rows, columns=DEAL_OPEN_COLS)
    df["created"] = pd.to_datetime(df["created"], errors="coerce").dt.date
    ts = pd.to_datetime(df["created_ts"], errors="coerce", utc=True)
    df["created_ts"] = ts.dt.tz_convert(TZ) if len(df) else ts
    mt_ = pd.to_datetime(df["modified_ts"], errors="coerce", utc=True)
    df["modified_ts"] = mt_.dt.tz_convert(TZ) if len(df) else mt_
    cm = snap.get("dcm") or {}
    df["comments"] = df["id"].map(lambda i: (cm.get(str(i)) or []) if i else [])
    return df
SOURCE_GROUPS_DEFAULT = {"STT": ["sale tự tìm"], "HOTLINE/CHAT": ["chat", "phone", "hotline", "điện thoại", "cuộc gọi", "call"],
                         "PARTNER": ["partner"], "UP SELL": ["up sell", "upsell"]}
SOURCE_GROUP_ORDER = ["STT", "MKT", "HOTLINE/CHAT", "PARTNER", "UP SELL"]


def source_group(src) -> str:
    """Nhóm source cho bảng doanh thu: STT / HOTLINE/CHAT / PARTNER / UP SELL, còn lại là MKT."""
    groups = dict(secret("bitrix24", "source_groups") or {}) or SOURCE_GROUPS_DEFAULT
    t = _plain(src)
    for g, kws in groups.items():
        if any(_plain(k) and _plain(k) in t for k in kws):
            return g
    return "MKT"


def _norm_txt(t) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", str(t).lower().replace("đ", "d"))
    return " ".join(sorted(re.findall(r"[a-z0-9]+", "".join(c for c in t if unicodedata.category(c) != "Mn"))))


def is_excluded_source(src) -> bool:
    return _norm_txt(src) in {_norm_txt(x) for x in lead_exclude_sources()}


def lead_stage_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Số lead theo sale × stage, đã bỏ các nguồn trong lead_exclude_sources (dùng cho trang Tổng quan)."""
    keep = detail[~detail["source"].map(is_excluded_source).astype(bool)] if not detail.empty else detail
    return keep.groupby(["sale", "stage"], as_index=False)["n"].sum()[["sale", "stage", "n"]]

def _cfg_list(v) -> list[str]:
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v]) if str(x).strip()]


HARA_SNAP = "sales-dashboard-haracrm.json"
HARA_VERSION = 5
HARA_SRC = r"""(async()=>{
const C=__CFG__;
const box=document.createElement('div');
box.style.cssText='position:fixed;z-index:2147483647;top:16px;right:16px;width:380px;padding:16px;background:white;color:rgb(34,34,34);border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.25);font:14px/1.5 sans-serif;white-space:pre-line';
document.body.appendChild(box);
const say=t=>{box.innerText='📈 Cập nhật HaraCRM → Dashboard\n'+t;};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const T=e=>String((e&&(e.innerText||e.textContent))||'').replace(/\s+/g,' ').trim();
const vis=e=>!!(e&&(e.offsetWidth||e.offsetHeight||e.getClientRects().length));
const Tl=e=>String((e&&(e.innerText||e.textContent))||'').split('\n').map(x=>x.replace(/\s+/g,' ').trim()).filter(Boolean).join('\n');
const rgb=v=>{const m=String(v||'').match(/rgba?\(([^)]+)\)/);if(!m)return null;const a=m[1].split(',').map(x=>parseFloat(x));return a.length>3&&a[3]<0.05?null:a;};
const isRed=c=>{for(const x of [c,...c.querySelectorAll('*')].slice(0,5)){const cs=getComputedStyle(x);const b=rgb(cs.backgroundColor);if(b&&b[0]>=200&&b[0]-b[1]>=20&&b[0]-b[2]>=20)return 1;const f=rgb(cs.color);if(f&&f[0]>=150&&f[1]<100&&f[2]<100)return 1;}return 0;};
const norm=t=>String(t||'').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[̀-ͯ]/g,'').split(/[^a-z0-9.]+/).filter(Boolean).join(' ');
const names=(C.names||[]).map(norm);const team=norm(C.team);
const roots=[];const addRoot=r=>{if(!r||roots.includes(r))return;roots.push(r);for(const e of r.querySelectorAll('*')){if(e.shadowRoot)addRoot(e.shadowRoot);if(e.tagName==='IFRAME'){try{if(e.contentDocument)addRoot(e.contentDocument);}catch(er){}}}};
const qa=sel=>{roots.length=0;addRoot(document);return roots.flatMap(r=>[...r.querySelectorAll(sel)]);};
const CELL='[role=cell],[role=gridcell],[role=columnheader],[role=rowheader],td,th';
const hit=cells=>cells.some(c=>{const n=norm(c);return n===team||n.indexOf(team+' ')===0||names.includes(n)||names.some(x=>n.endsWith(' '+x));});
const titleOf=t=>{let e=t;for(let i=0;i<7;i++){e=e.parentElement;if(!e)break;const hs=[...e.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(h=>!t.contains(h)&&(h.compareDocumentPosition(t)&Node.DOCUMENT_POSITION_FOLLOWING));if(hs.length){const h=hs[hs.length-1];let sub='';const nx=h.nextElementSibling;if(nx&&!nx.contains(t)&&nx.tagName!=='TABLE'){sub=T(nx).slice(0,240);}return [T(h),sub];}}return ['Bảng',''];};
const summ=c=>c.slice(0,2).some(x=>/^(tb|tong|trung binh)( |$)/.test(norm(x)));
const scrape=tab=>{const out=[];for(const t of qa('table,[role=table],[role=grid],[role=treegrid]')){if(!vis(t)||(t.tagName!=='TABLE'&&t.querySelector('table')))continue;const isT=t.tagName==='TABLE';const trs=isT?[...t.querySelectorAll('tr')]:[...t.querySelectorAll('[role=row]')];if(!trs.length)continue;const kids=r=>isT?[...r.children]:[...r.querySelectorAll(CELL)];const thr=isT?[...t.querySelectorAll('thead tr')]:[];const hr=(thr.length?thr.reduce((a,b)=>kids(b).length>=kids(a).length?b:a):null)||trs.find(r=>r.querySelector('th,[role=columnheader]'))||trs[0];const headers=kids(hr).map(Tl);const body=trs.filter(r=>r!==hr&&!(r.parentElement&&r.parentElement.tagName==='THEAD')&&vis(r)).map(r=>{const k=kids(r);return {c:k.map(Tl),f:k.map(isRed)};}).filter(x=>x.c.length);const mine=body.filter(x=>hit(x.c));if(!mine.length)continue;const keep=body.filter(x=>hit(x.c)||summ(x.c));const [title,sub]=titleOf(t);out.push({tab:tab,title:title,sub:sub,headers:headers,rows:keep.map(x=>x.c),flags:keep.map(x=>x.f)});}return out;};
const showSmall=async()=>{if(C.small===false)return false;const cb=qa('input[type=checkbox]').filter(e=>{const l=e.closest('label')||e.parentElement;return l&&vis(l)&&norm(T(l)).indexOf('mau nho')>=0;}).find(e=>!e.checked);if(!cb)return false;cb.click();await sleep(1200);return true;};
const tabEl=name=>{const n=norm(name);return qa('button,a,[role=tab],li,span,div').filter(e=>vis(e)&&e.children.length<=2&&norm(T(e))===n).pop();};
try{
const getFilt=()=>{const dx=qa('span,div,p').filter(vis).map(T).filter(x=>x.length<300&&x.indexOf('Đang xem')>=0).map(x=>(x.match(/Đang xem:?\s*([0-9/]+\s*[–-]\s*[0-9/]+)/)||[])[1]).find(Boolean);return dx||qa('button,span,div,a').filter(vis).map(T).find(x=>x.length<60&&/^\d{1,2}\/\d{1,2}\s*[–-]\s*\d{1,2}\/\d{1,2}/.test(x))||'';};
const readAll=async()=>{const tables=[];const seen=new Set();const miss=[];for(const name of (C.tabs||[])){const el=tabEl(name);if(!el){miss.push(name);continue;}say('Đang mở tab '+name+'… (chờ bảng hiện ra)');el.click();let got=[];for(let i=0;i<Math.ceil((C.wait||15000)/500);i++){await sleep(500);got=scrape(name);if(got.length){await sleep(800);got=scrape(name);break;}}if(got.length&&await showSmall())got=scrape(name);for(const x of got){const k=x.title+'|'+x.headers.join('|');if(!seen.has(k)){seen.add(k);tables.push(x);}}}if(!tables.length){for(const x of scrape('Trang hiện tại')){const k=x.title+'|'+x.headers.join('|');if(!seen.has(k)){seen.add(k);tables.push(x);}}}return {tables:tables,miss:miss};};
const curMonth=()=>{const f=getFilt();const m=(f.match(/\d{1,2}\/(\d{1,2})\/\d{4}/)||f.match(/^\s*\d{1,2}\/(\d{1,2})/)||[])[1];return m?+m:0;};
const mBtn=m=>qa('button,a,span,div,li').filter(e=>vis(e)&&e.children.length<=1&&T(e)==='T'+m).pop();
const openFilt=async()=>{const b=qa('button,a').filter(vis).find(e=>norm(T(e))==='doi bo loc');if(b){b.click();await sleep(900);}};
const periods=[];let miss=[];const nowD=new Date();const want=[];for(let k=(C.months||2)-1;k>=0;k--){want.push(new Date(nowD.getFullYear(),nowD.getMonth()-k,1));}
for(const d of want){const m=d.getMonth()+1;const label='Tháng '+m;const key=d.getFullYear()+'-'+String(m).padStart(2,'0');let btn=curMonth()===m?null:mBtn(m);if(!btn&&curMonth()!==m){await openFilt();btn=mBtn(m);}if(btn){say('Đang chọn '+label+' trên HaraCRM…');btn.click();let ok=false;for(let i=0;i<40;i++){await sleep(500);if(curMonth()===m){ok=true;break;}if(i===6){const ap=qa('button').filter(vis).find(e=>T(e)==='Áp dụng');if(ap)ap.click();}}if(!ok){periods.push({key:key,label:label,filter:getFilt(),tables:[],err:'Không chuyển được sang '+label});continue;}await sleep(1500);}else if(want.length>1&&curMonth()!==m){periods.push({key:key,label:label,filter:getFilt(),tables:[],err:'Không thấy nút T'+m});continue;}const srcs=C.sources||[];const sAll=srcs.map(norm);
const srcBtn=l=>qa('button,a,span,div,label,li').filter(e=>vis(e)&&e.children.length<=1&&norm(T(e))===norm(l)&&e.parentElement&&sAll.every(x=>norm(T(e.parentElement.parentElement||e.parentElement)).indexOf(x)>=0)).sort((a,b)=>T(a.parentElement.parentElement||a.parentElement).length-T(b.parentElement.parentElement||b.parentElement).length)[0];
const dx=()=>qa('span,div,p').filter(vis).map(T).filter(x=>x.length<300&&x.indexOf('Đang xem')>=0).sort((a,b)=>a.length-b.length)[0]||'';
const ptab=[];let srcErr='';
if(srcs.length&&srcBtn(srcs[0])){for(const sl of srcs){const b=srcBtn(sl);if(!b){srcErr+='Không thấy nút nguồn «'+sl+'». ';continue;}const before=dx();say('Đang đọc '+label+' · nguồn «'+sl+'»…');b.click();for(let i=0;i<12;i++){await sleep(500);if(dx()!==before)break;}await sleep(1200);const r=await readAll();miss=r.miss;for(const x of r.tables){x.src=sl;ptab.push(x);}}
const b0=srcBtn(srcs[0]);if(b0){b0.click();await sleep(1500);}}
else{say('Đang đọc '+label+'…');const r=await readAll();miss=r.miss;for(const x of r.tables)ptab.push(x);if(srcs.length)srcErr='Không thấy nút chọn Nguồn — chỉ lấy được nguồn đang chọn.';}
periods.push({key:key,label:label,filter:getFilt(),tables:ptab,srcErr:srcErr});}
const tables=periods.flatMap(p=>p.tables);const filt=periods.length?periods[periods.length-1].filter:getFilt();
if(!tables.length){qa('x');const nT=qa('table').length,nR=qa('[role=table],[role=grid]').length,nI=document.querySelectorAll('iframe').length,nS=roots.length-1,has=roots.some(r=>norm(T(r.body||r)).indexOf(team)>=0);say('❌ Không thấy bảng nào có dòng của '+C.team+' trên trang này.\nChẩn đoán: bảng HTML '+nT+' · role=table '+nR+' · iframe '+nI+' · vùng con '+nS+' · thấy chữ '+C.team+': '+(has?'có':'không')+'.\nChụp khung này gửi lại để chỉnh nút.');const x=document.createElement('button');x.innerText='Đóng';x.onclick=()=>box.remove();box.appendChild(x);return;}
const data={v:3,kind:'hara',bv:C.bv,at:Date.now(),host:location.host,filter:filt,team:C.team,missTabs:miss,periods:periods,tables:periods.length?periods[periods.length-1].tables:[]};
const b64u=buf=>{let t='';const u8=new Uint8Array(buf);for(let i=0;i<u8.length;i+=8192){t+=String.fromCharCode.apply(null,u8.subarray(i,i+8192));}return btoa(t).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');};
const raw=new TextEncoder().encode(JSON.stringify(data));
let b64;try{const cs=new CompressionStream('deflate-raw');const wr=cs.writable.getWriter();wr.write(raw);wr.close();b64='z'+b64u(await new Response(cs.readable).arrayBuffer());}catch(e){b64=b64u(raw);}
const viaUrl=(C.url+'?hx='+b64).length<6000;
say('✅ Đã đọc: '+periods.map(p=>p.label+' '+(p.err?'(⚠️ '+p.err+')':p.tables.length+' bảng')).join(' · ')+'.'+(miss.length?'\n⚠️ Không thấy tab: '+miss.join(', '):'')+'\nBấm nút dưới để gửi sang dashboard.');
const btn=document.createElement('button');btn.innerText='📤 Gửi sang dashboard';btn.style.cssText='margin-top:10px;padding:8px 14px;background:rgb(42,120,214);color:white;border:0;border-radius:6px;font-weight:600;cursor:pointer';
const ta=document.createElement('textarea');ta.value=b64;ta.readOnly=true;ta.style.cssText='display:block;width:340px;height:60px;margin-top:8px;font:11px monospace';
const x=document.createElement('button');x.innerText='Đóng';x.style.cssText='margin:10px 0 0 10px;padding:8px 14px;background:white;border:1px solid rgb(200,200,200);border-radius:6px;cursor:pointer';x.onclick=()=>box.remove();
window.addEventListener('message',e=>{if(e.data==='bx-ready'&&e.source){try{e.source.postMessage({bx:b64},'*');}catch(er){}}else if(e.data==='bx-saved'){say('✅ Dashboard đã nhận dữ liệu và đang lưu. Có thể đóng khung này.');box.appendChild(x);setTimeout(()=>box.remove(),8000);}});
btn.onclick=async()=>{window.open(viaUrl?C.url+'?hx='+b64:C.url+'?paste=1','_blank');if(viaUrl){box.remove();return;}let ok=false;try{await navigator.clipboard.writeText(b64);ok=true;}catch(e){}say('📤 Đang gửi sang dashboard (tab mới)… thường tự xong sau vài giây.\nNếu dashboard không tự lưu: '+(ok?'bấm vào ô dán trên dashboard, nhấn Cmd/Ctrl + V rồi bấm Lưu.':'bấm vào ô dưới, nhấn Cmd/Ctrl + A rồi Cmd/Ctrl + C, sang dashboard dán vào ô và bấm Lưu.'));box.appendChild(ta);ta.select();box.appendChild(x);};
box.appendChild(btn);box.appendChild(x);
}catch(e){say('❌ Lỗi: '+e.message);const x=document.createElement('button');x.innerText='Đóng';x.onclick=()=>box.remove();box.appendChild(x);}
})();"""


HARA_SOURCES = ["Tất cả", "Nguồn công ty", "Sale tự tìm"]
VC_SNAP = "sales-dashboard-voicecloud.json"
VC_VERSION = 1
VC_EXT_DEFAULT = {"1226": "Hiếu", "1021": "Toàn", "1177": "Nhi", "1008": "Châu"}   # máy lẻ VoiceCloud → Sale
VC_SRC = r"""(async()=>{
const C=__CFG__;
const box=document.createElement('div');
box.style.cssText='position:fixed;z-index:2147483647;top:16px;right:16px;width:380px;padding:16px;background:white;color:rgb(34,34,34);border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.25);font:14px/1.5 sans-serif;white-space:pre-line';
document.body.appendChild(box);
const say=t=>{box.innerText='📞 Cập nhật VoiceCloud → Dashboard\n'+t;};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const T=e=>String((e&&(e.innerText||e.textContent))||'').replace(/\s+/g,' ').trim();
const vis=e=>!!(e&&(e.offsetWidth||e.offsetHeight||e.getClientRects().length));
const norm=t=>String(t||'').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[̀-ͯ]/g,'').split(/[^a-z0-9]+/).filter(Boolean).join(' ');
const closeBtn=()=>{const x=document.createElement('button');x.innerText='Đóng';x.style.cssText='margin:10px 0 0 10px;padding:8px 14px;background:white;border:1px solid rgb(200,200,200);border-radius:6px;cursor:pointer';x.onclick=()=>box.remove();return x;};
try{
const pad=n=>String(n).padStart(2,'0');const now=new Date();
const f0=new Date(now.getFullYear(),now.getMonth()-((C.months||2)-1),1);
const fmt=(d,hm)=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+hm;
const isDT=v=>/^\d{4}-\d{2}-\d{2}/.test(String(v||'').trim());
const dts=()=>[...document.querySelectorAll('input')].filter(i=>vis(i)&&isDT(i.value));
const setV=(el,v)=>{const s=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;s.call(el,v);for(const ev of ['input','change','blur'])el.dispatchEvent(new Event(ev,{bubbles:true}));try{if(window.jQuery){jQuery(el).val(v).trigger('change');}}catch(e){}};
const tableOf=doc=>[...doc.querySelectorAll('table')].find(t=>{const h=norm(T(t.querySelector('thead')||t.rows[0]));return h.indexOf('caller id')>=0&&h.indexOf('calldate')>=0;});
const firstRow=()=>{const t=tableOf(document);const r=t&&t.querySelector('tbody tr');return r?T(r):'';};
const waitChange=async(before,ms)=>{for(let i=0;i<ms/400;i++){await sleep(400);if(firstRow()!==before)return true;}return false;};
const btns=()=>[...document.querySelectorAll('button,a,input[type=submit],input[type=button]')].filter(vis);
const searchBtn=()=>{const all=btns();const ex=all.find(b=>norm(T(b)||b.value)==='export');return all.find(b=>b.querySelector('.fa-search,.ti-search,.bi-search,[class*=search]'))||(ex&&ex.previousElementSibling&&vis(ex.previousElementSibling)?ex.previousElementSibling:null)||all.find(b=>/search|tim/.test(norm(T(b)||b.value)))||document.querySelector('form button[type=submit]');};
let d=dts();
if(d.length>=2&&d[0].value.trim().slice(0,10)>fmt(f0,'').trim()){
  say('Đang đặt khoảng ngày '+fmt(f0,'00:00')+' → hôm nay rồi bấm tìm…\nNếu trang tải lại, hãy bấm nút 📞 lần nữa.');
  setV(d[0],fmt(f0,'00:00'));setV(d[1],fmt(now,'23:59'));await sleep(400);
  const before=firstRow();const sb=searchBtn();if(!sb)throw new Error('Không thấy nút tìm (🔍) trên trang.');sb.click();
  await waitChange(before,20000);await sleep(800);
}
d=dts();const from=(d[0]&&d[0].value.trim())||'',to=(d[1]&&d[1].value.trim())||'';
try{for(const s of [...document.querySelectorAll('select')].filter(vis)){const ops=[...s.options].map(o=>o.value);if(ops.length>=2&&ops.every(v=>/^-?\d+$/.test(v))&&ops.some(v=>+v>=25)){const mx=ops.includes('-1')?'-1':ops.reduce((a,b)=>+b>+a?b:a);if(s.value!==mx){const b=firstRow();s.value=mx;s.dispatchEvent(new Event('change',{bubbles:true}));try{if(window.jQuery)jQuery(s).trigger('change');}catch(e){}await waitChange(b,15000);await sleep(600);}}}}catch(e){}
const ext=C.ext||{};const calls={},other={};const seen=new Set();let total=0,pages=0,firstDay='',lastDay='';
const read=doc=>{const t=tableOf(doc);if(!t)return 0;const hs=[...(t.querySelector('thead tr:last-child')||t.rows[0]).children].map(c=>norm(T(c)));const ix=k=>hs.findIndex(h=>h===k||h.indexOf(k)>=0);
const iD=ix('calldate'),iE=ix('caller id'),iDst=ix('destination'),iDur=ix('duration'),iTalk=ix('time calling'),iSt=ix('status');let n=0;
for(const r of t.querySelectorAll('tbody tr')){const c=[...r.children].map(T);if(c.length<4)continue;const day=(String(c[iD]||'').replace(/\s+/g,'').match(/\d{4}-\d{2}-\d{2}/)||[])[0];if(!day)continue;
const key=c.slice(0,Math.min(c.length,10)).join('|');if(seen.has(key))continue;seen.add(key);n++;total++;
const e=String(c[iE]||'').trim();const talk=parseFloat(c[iTalk])||0;const ok=/answer/i.test(c[iSt]||'')?1:0;
if(!firstDay||day<firstDay)firstDay=day;if(!lastDay||day>lastDay)lastDay=day;
const s=ext[e];if(!s){other[e]=(other[e]||0)+1;continue;}const m=calls[s]||(calls[s]={});const v=m[day]||(m[day]=[0,0,0]);v[0]++;v[1]+=ok;v[2]+=talk;}return n;};
const nextOf=doc=>{const c=[...doc.querySelectorAll('a,button,li')].filter(e=>{const tx=T(e);const lab=norm(e.getAttribute('aria-label')||e.getAttribute('title')||'');return (['»','›','>','→','Next','Sau','Tiếp','next'].includes(tx)||lab==='next'||lab.indexOf('next')===0)&&e.children.length<=2;});
for(const e of c){const a=e.tagName==='LI'?e.querySelector('a,button'):e;if(!a)continue;const li=a.closest('li');const dis=(a.className+' '+(li?li.className:'')).indexOf('disabled')>=0||a.disabled||a.getAttribute('aria-disabled')==='true';if(!dis)return a;}return null;};
let doc=document,live=true;
for(let guard=0;guard<2000;guard++){pages++;const n=read(doc);say('Đang đọc trang '+pages+' · '+total+' cuộc gọi…');const nx=nextOf(doc);if(!nx)break;
const href=nx.getAttribute&&nx.getAttribute('href');
if(href&&href!=='#'&&!/^javascript/i.test(href)){const u=new URL(href,location.href);const h=await (await fetch(u,{credentials:'include'})).text();doc=new DOMParser().parseFromString(h,'text/html');live=false;if(!tableOf(doc))break;}
else{if(!live)break;const b=firstRow();nx.click();if(!(await waitChange(b,15000)))break;await sleep(300);}
if(n===0&&pages>3)break;}
if(!total){say('❌ Không đọc được cuộc gọi nào. Mở trang Report → Outgoings, bấm 🔍 để hiện danh sách rồi bấm nút lần nữa.');box.appendChild(closeBtn());return;}
const data={v:1,kind:'vc',bv:C.bv,at:Date.now(),host:location.host,from:from||firstDay,to:to||lastDay,firstDay:firstDay,lastDay:lastDay,rows:total,pages:pages,calls:calls,sales:[...new Set(Object.values(ext))],other:other};
const b64u=buf=>{let t='';const u8=new Uint8Array(buf);for(let i=0;i<u8.length;i+=8192){t+=String.fromCharCode.apply(null,u8.subarray(i,i+8192));}return btoa(t).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');};
const raw=new TextEncoder().encode(JSON.stringify(data));let b64;try{const cs=new CompressionStream('deflate-raw');const wr=cs.writable.getWriter();wr.write(raw);wr.close();b64='z'+b64u(await new Response(cs.readable).arrayBuffer());}catch(e){b64=b64u(raw);}
const viaUrl=(C.url+'?vx='+b64).length<6000;
const per=Object.entries(calls).map(([s,m])=>s+' '+Object.values(m).reduce((a,v)=>a+v[0],0)).join(' · ');
const oth=Object.values(other).reduce((a,b)=>a+b,0);
say('✅ Đã đọc '+total+' cuộc gọi đi ('+pages+' trang, '+(from||firstDay)+' → '+(to||lastDay)+').\n'+per+(oth?'\n(Bỏ qua '+oth+' cuộc của máy lẻ khác)':'')+'\nBấm nút dưới để gửi sang dashboard.');
const btn=document.createElement('button');btn.innerText='📤 Gửi sang dashboard';btn.style.cssText='margin-top:10px;padding:8px 14px;background:rgb(42,120,214);color:white;border:0;border-radius:6px;font-weight:600;cursor:pointer';
const ta=document.createElement('textarea');ta.value=b64;ta.readOnly=true;ta.style.cssText='display:block;width:340px;height:60px;margin-top:8px;font:11px monospace';
const x=closeBtn();
window.addEventListener('message',e=>{if(e.data==='bx-ready'&&e.source){try{e.source.postMessage({bx:b64},'*');}catch(er){}}else if(e.data==='bx-saved'){say('✅ Dashboard đã nhận dữ liệu và đang lưu. Có thể đóng khung này.');box.appendChild(x);setTimeout(()=>box.remove(),8000);}});
btn.onclick=async()=>{window.open(viaUrl?C.url+'?vx='+b64:C.url+'?paste=1','_blank');if(viaUrl){box.remove();return;}let ok=false;try{await navigator.clipboard.writeText(b64);ok=true;}catch(e){}say('📤 Đang gửi sang dashboard (tab mới)… thường tự xong sau vài giây.\nNếu dashboard không tự lưu: '+(ok?'bấm vào ô dán trên dashboard, nhấn Cmd/Ctrl + V rồi bấm Lưu.':'bấm vào ô dưới, nhấn Cmd/Ctrl + A rồi Cmd/Ctrl + C, sang dashboard dán vào ô và bấm Lưu.'));box.appendChild(ta);ta.select();box.appendChild(x);};
box.appendChild(btn);box.appendChild(x);
}catch(e){say('❌ Lỗi: '+e.message);box.appendChild(closeBtn());}
})();"""


def vc_ext_map(roster_: pd.DataFrame) -> dict[str, str]:
    """Máy lẻ → tên Sale đầy đủ trong danh sách (Secrets [voicecloud] extensions, chấp nhận tên ngắn như 'Nhi')."""
    raw = dict(secret("voicecloud_portal", "extensions", VC_EXT_DEFAULT) or {})
    names = list(roster_["sale"])
    out = {str(v).strip(): n for v, n in zip(roster_.get("vc_id", pd.Series(dtype=str)), roster_["sale"])
           if str(v or "").strip() and str(v).strip().lower() != "nan"}
    for ext_, nm in raw.items():
        z = _plain(str(nm))
        hit = next((n for n in names if _plain(n) == z), None) or next(
            (n for n in names if _plain(n).endswith(" " + z)), None)
        if hit:
            out[str(ext_).strip()] = hit
    return out


def vc_href(target_url: str, roster_: pd.DataFrame) -> str:
    from urllib.parse import quote

    cfg = {"url": target_url, "ext": vc_ext_map(roster_), "bv": VC_VERSION,
           "months": int(secret("voicecloud_portal", "months", 2))}
    js_ = "".join(VC_SRC.splitlines()).replace("__CFG__", json.dumps(cfg, ensure_ascii=False))
    return "javascript:" + quote(js_, safe="()[]{}=;:,.'!*+/?&<>|-_~@$")


def hara_href(target_url: str, roster: pd.DataFrame) -> str:
    from urllib.parse import quote

    cfg = {"url": target_url, "team": str(secret("haracrm", "team", "S2.4")),
           "tabs": list(secret("haracrm", "tabs", ["Điều hành", "Năng lực NV"])),
           "names": list(roster["sale"]), "wait": int(secret("haracrm", "wait_ms", 15000)), "bv": HARA_VERSION,
           "months": int(secret("haracrm", "months", 2)),
           "sources": list(secret("haracrm", "sources", HARA_SOURCES))}
    js = "".join(HARA_SRC.splitlines()).replace("__CFG__", json.dumps(cfg, ensure_ascii=False))
    return "javascript:" + quote(js, safe="()[]{}=;:,.'!*+/?&<>|-_~@$")


BOOKMARKLET_VERSION = 22  # tăng mỗi khi đổi nút → dashboard nhắc cài lại nút


def bookmarklet_href(target_url: str, roster: pd.DataFrame) -> str:
    from urllib.parse import quote

    stages, _ = lead_stage_config()
    cfg = {"url": target_url, "months": int(secret("bitrix24", "months", 3)), "stages": stages,
           "users": [[r.bitrix_id, r.sale, r.email] for r in roster.itertuples()],
           "excl": lead_exclude_sources(), "closed": lead_stage_config()[1],
           "reason": _cfg_list(secret("bitrix24", "unqual_reason_field", "")),
           "need": _cfg_list(secret("bitrix24", "need_field", NEED_FIELDS_DEFAULT)),
           "needRe": str(secret("bitrix24", "need_regex", "") or ""),
           "revDate": str(secret("bitrix24", "revenue_date_field", "Ngày ghi nhận doanh số") or "Ngày ghi nhận doanh số"),
           "lostReason": str(secret("bitrix24", "lost_reason_field", "") or ""), "bv": BOOKMARKLET_VERSION,
           "comments": bool(secret("bitrix24", "read_comments", True)), "cmExcl": comment_exclude(),
           "pkg": str(secret("bitrix24", "package_field", "") or ""),
           "calls": bool(secret("bitrix24", "read_calls", True)),
           "callTypes": [int(x) for x in secret("bitrix24", "call_types", [])]}
    js = "".join(BOOKMARKLET_SRC.splitlines())
    js = js.replace("__CFG__", json.dumps(cfg, ensure_ascii=False))
    return "javascript:" + quote(js, safe="()[]{}=;:,.'!*+/?&<>|-_~@$")


@st.cache_data(ttl=CACHE_TTL, show_spinner="Đang tải Meeting từ lịch của leader…")
def load_meetings_leader(emails: tuple[str, ...], sales: tuple[str, ...], d_start: date, d_end: date) -> pd.DataFrame:
    cfg = dict(secret("google_calendar") or {})
    internal = [d.lower().lstrip("@") for d in cfg.get("internal_domains", [])]
    team = {e.lower(): s for e, s in zip(emails, sales) if e}
    leader = str(secret("google_oauth", "leader_email", "")).lower()
    t0, t1 = to_dt_range(d_start, d_end)
    svc = _leader_service("calendar", "v3")
    rows, token = [], None
    while True:
        res = svc.events().list(calendarId="primary", timeMin=t0.isoformat(), timeMax=t1.isoformat(),
                                singleEvents=True, orderBy="startTime", maxResults=2500, pageToken=token).execute()
        for ev in res.get("items", []):
            people = [ev.get("organizer", {}).get("email", "").lower()] + \
                     [a.get("email", "").lower() for a in ev.get("attendees", [])]
            owner = next((e for e in people if e in team and e != leader), None) or \
                    next((e for e in people if e in team), None)
            if not owner:
                continue
            g = [x for x in _guests(ev, owner, internal) if x not in team and x != leader]
            if _is_meeting(ev, owner, g, cfg):
                rows.append({"event_id": ev["id"], "sale": team[owner], "email": owner,
                             "ts": ev["start"]["dateTime"], "end": ev["end"]["dateTime"],
                             "title": ev.get("summary", "(không tiêu đề)"), "guests": ", ".join(g),
                             "deal_ref": deal_ref(ev)})
        token = res.get("nextPageToken")
        if not token:
            break
    df = pd.DataFrame(rows, columns=["event_id", "sale", "email", "ts", "end", "title", "guests", "deal_ref"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ)
    df["end"] = pd.to_datetime(df["end"], utc=True).dt.tz_convert(TZ)
    return df


@st.cache_data(ttl=CACHE_TTL, show_spinner="Đang tải Meeting từ Google Calendar…")
def load_meetings(emails: tuple[str, ...], sales: tuple[str, ...], d_start: date, d_end: date) -> pd.DataFrame:
    cfg = dict(secret("google_calendar") or {})
    delegate = bool(cfg.get("domain_wide_delegation", False))
    internal = [d.lower().lstrip("@") for d in cfg.get("internal_domains", [])]
    t0, t1 = to_dt_range(d_start, d_end)
    rows, errors = [], []
    for email, sale in zip(emails, sales):
        if not email:
            continue
        try:
            svc = _google_service("calendar", "v3", ["https://www.googleapis.com/auth/calendar.readonly"],
                                  subject=email if delegate else None)
            token = None
            while True:
                res = svc.events().list(
                    calendarId="primary" if delegate else email, timeMin=t0.isoformat(), timeMax=t1.isoformat(),
                    singleEvents=True, orderBy="startTime", maxResults=2500, pageToken=token).execute()
                for ev in res.get("items", []):
                    g = _guests(ev, email, internal)
                    if _is_meeting(ev, email, g, cfg):
                        rows.append({"event_id": ev["id"], "sale": sale, "email": email,
                                     "ts": ev["start"]["dateTime"], "end": ev["end"]["dateTime"],
                                     "title": ev.get("summary", "(không tiêu đề)"), "guests": ", ".join(g),
                             "deal_ref": deal_ref(ev)})
                token = res.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            errors.append(f"{sale}: {e}")
    df = pd.DataFrame(rows, columns=["event_id", "sale", "email", "ts", "end", "title", "guests", "deal_ref"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(TZ)
    df["end"] = pd.to_datetime(df["end"], utc=True).dt.tz_convert(TZ)
    df.attrs["errors"] = errors
    return df


def extract_estimate_amount(text: str) -> float:
    if not text:
        return 0.0
    patterns = [
        r"([\d.,]+)\s*(?:tỷ|ty)",
        r"([\d.,]+)\s*(?:tr|triệu|trieu)",
        r"([\d.,]+)\s*(?:k|nghìn)",
        r"([\d.,]+)\s*(?:đ|vnđ|vnd|đồng)"
    ]
    
    for p in patterns:
        matches = re.findall(p, text.lower())
        if matches:
            val_str = matches[0].replace(".", "").replace(",", ".")
            try:
                num = float(val_str)
                if "tỷ" in p or "ty" in p:
                    return num * 1e9
                elif "tr" in p or "triệu" in p or "trieu" in p:
                    return num * 1e6
                elif "k" in p or "nghìn" in p:
                    return num * 1e3
                else:
                    return num
            except ValueError:
                continue
                
    match_large = re.findall(r"(?:giá|bao gia|tong|total|est|estimate|báo giá)[:\s]*([\d.,]+)", text.lower())
    if match_large:
        val_str = match_large[0].replace(".", "").replace(",", "")
        try:
            return float(val_str)
        except ValueError:
            pass
            
    return 0.0


@st.cache_data(ttl=CACHE_TTL, show_spinner="Đang tải email recap từ Gmail…")
def load_recap_emails(emails: tuple[str, ...], d_start: date, d_end: date, leader: bool = False) -> pd.DataFrame:
    cfg = dict(secret("gmail") or {})
    default_kws = ["haravan", "đề xuất", "recap"]
    user_kws = [k for k in cfg.get("subject_keywords", []) if k]
    kws = list(set(user_kws + default_kws))
    
    subj = " subject:(" + " OR ".join(f'"{k}"' for k in kws) + ")" if kws else ""
    cc = str(cfg.get("recap_cc", "")).strip().lower()
    t0, _ = to_dt_range(d_start, d_end)
    t1_now = datetime.now(TZ) + timedelta(hours=1)
    
    rows, errors, svc = [], [], None
    for email in emails:
        if not email:
            continue
        try:
            if leader:
                svc = svc or _leader_service("gmail", "v1")
                s_ = svc
            else:
                s_ = _google_service("gmail", "v1", ["https://www.googleapis.com/auth/gmail.readonly"], subject=email)
            parts = [f"from:{email}" if leader else "in:sent"]
            if cc and cc != email.lower():
                parts.append(f"cc:{cc}")
            q = " ".join(parts + [f"after:{int(t0.timestamp())}", f"before:{int(t1_now.timestamp())}"]) + subj
            ids, token = [], None
            while len(ids) < 500:
                res = s_.users().messages().list(userId="me", q=q, maxResults=100, pageToken=token).execute()
                ids += [m["id"] for m in res.get("messages", [])]
                token = res.get("nextPageToken")
                if not token:
                    break
            for mid in ids:
                m = s_.users().messages().get(userId="me", id=mid, format="full").execute()
                h = {x["name"].lower(): x["value"] for x in m.get("payload", {}).get("headers", [])}
                
                snippet = m.get("snippet", "")
                subject_text = h.get("subject", "")
                combined_text = subject_text + " " + snippet
                est_amount = extract_estimate_amount(combined_text)
                
                rcpt = re.findall(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", (h.get("to", "") + "," + h.get("cc", "")).lower())
                rows.append({
                    "sender": email.lower(), 
                    "ts": int(m.get("internalDate", 0)) / 1000,
                    "subject": subject_text, 
                    "rcpt": ",".join(rcpt), 
                    "est_amount": est_amount,
                    "message_id": mid,
                    "rfc_id": h.get("message-id", "").strip().strip("<>"),
                })
        except Exception as e:
            errors.append(f"{email}: {e}")
    df = pd.DataFrame(rows, columns=["sender", "ts", "subject", "rcpt", "est_amount", "message_id", "rfc_id"])
    df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(TZ)
    df.attrs["errors"] = sorted(set(errors))[:20]
    return df


NAME_IGNORE_DEFAULT = [
    "meeting", "demo", "recap", "haravan", "gui", "gap", "hop", "voi", "va", "cung", "call", "online", "offline",
    "zoom", "google", "meet", "cuoc", "buoi", "lich", "hen", "tom", "tat", "follow", "up", "followup",
    "anh", "chi", "em", "co", "chu", "bac", "ong", "ba", "mr", "ms", "mrs", "a", "c", "e",
]


def title_name_tokens(title: str) -> set[str]:
    extra = [norm_text(w) for w in secret("gmail", "name_ignore_words", []) or []]
    kws = [norm_text(k) for k in (secret("google_calendar", "title_keywords", []) or [])]
    ignore = set(NAME_IGNORE_DEFAULT) | set(extra) | {t for k in kws for t in tokens(k)}
    return {t for t in tokens(title) if t not in ignore and not t.isdigit()}


DEAL_TITLE_IGNORE = ["haravan", "official", "hotchat", "website", "web", "shop", "store", "x", "va", "and"]
NEGOTIATION_WORDS = ["negotiation", "dam phan", "bao gia", "quotation", "signed", "contract", "hop dong", "ky"]


def match_meeting_deals(meetings: pd.DataFrame, deals: pd.DataFrame) -> pd.DataFrame:
    """Nối meeting ↔ deal Bitrix: (1) link deal sale dán trong mô tả lịch; (2) tự khớp tên trong tiêu đề meeting
    với tên deal của cùng Sale. Trả về cột kết quả cho từng meeting."""
    cols = ["event_id", "deal_id", "deal_title", "deal_stage", "deal_amount", "deal_link", "deal_untouched",
            "outcome", "deal_by", "deal_comments"]
    if meetings.empty:
        return pd.DataFrame(columns=cols)
    ign = set(NAME_IGNORE_DEFAULT) | set(DEAL_TITLE_IGNORE)
    by_id = {str(r.id): r for r in deals.itertuples()} if not deals.empty else {}
    by_sale = {k: g for k, g in deals.groupby("sale")} if not deals.empty else {}
    dtoks = {str(r.id): {t for t in tokens(r.title) if t not in ign and not t.isdigit()} for r in deals.itertuples()}
    rank = {"open": 0, "won": 0, "lost": 1}
    now_ts = pd.Timestamp(datetime.now(TZ))
    out = []
    for m in meetings.itertuples():
        d, how = None, ""
        ref = str(getattr(m, "deal_ref", "") or "")
        if ref and ref in by_id:
            d, how = by_id[ref], "link trong lịch"
        else:
            name = title_name_tokens(m.title)
            best, best_sc = None, 0.0
            if name:
                for r in by_sale.get(m.sale, pd.DataFrame(columns=DEAL_ALL_COLS)).itertuples():
                    common = name & dtoks.get(str(r.id), set())
                    if not common:
                        continue
                    sc = len(common) / len(name) - 0.01 * rank.get(r.status, 1)
                    if sc > best_sc:
                        best, best_sc = r, sc
            if best is not None and best_sc >= 0.5:
                d, how = best, "tên (tự khớp)"
        if d is None:
            out.append({"event_id": m.event_id, "deal_id": ref, "deal_title": "", "deal_stage": "", "deal_amount": None,
                        "deal_link": "", "deal_untouched": None,
                        "outcome": "⚪ Chưa có trong dữ liệu" if ref else "⚪ Chưa nối deal", "deal_by": "",
                        "deal_comments": []})
            continue
        unt = None
        if d.status == "open" and not pd.isna(d.modified_ts):
            unt = round((now_ts - pd.Timestamp(d.modified_ts)).total_seconds() / 86400, 1)
        if d.status == "won":
            oc = "🟢 Đã chốt"
        elif d.status == "lost":
            oc = "⚫ Lost"
        elif any(w in norm_text(d.stage) for w in NEGOTIATION_WORDS):
            oc = "🔵 Đang đàm phán"
        elif unt is not None and unt > 14:
            oc = "🔴 Nguội"
        else:
            oc = "🟡 Đang theo"
        out.append({"event_id": m.event_id, "deal_id": str(d.id), "deal_title": d.title,
                    "deal_stage": d.stage + (f" · {d.lost_reason}" if d.status == "lost" and d.lost_reason else ""),
                    "deal_amount": d.amount, "deal_link": d.link, "deal_untouched": unt, "outcome": oc, "deal_by": how,
                    "deal_comments": getattr(d, "comments", None) or []})
    return pd.DataFrame(out, columns=cols)


OUTCOME_ORDER = ["🟢 Đã chốt", "🔵 Đang đàm phán", "🟡 Đang theo", "🔴 Nguội", "⚫ Lost", "⚪ Chưa nối deal",
                 "⚪ Chưa có trong dữ liệu"]


def gmail_link(rfc_id: str, msg_id: str = "") -> str:
    """Link mở email trong hộp thư CỦA NGƯỜI BẤM: tìm theo Message-ID (giống nhau ở hộp thư người gửi, người nhận
    và người được CC), nên sale bấm sẽ ra mail trong mục Đã gửi của mình, leader ra mail được CC."""
    from urllib.parse import quote
    rfc_id = str(rfc_id or "").strip().strip("<>")
    if rfc_id and rfc_id.lower() != "nan":
        return "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{rfc_id}", safe="")
    return f"https://mail.google.com/mail/u/0/#all/{msg_id}" if msg_id else "https://mail.google.com/"



def match_recaps(meetings: pd.DataFrame, emails: pd.DataFrame) -> pd.DataFrame:
    g = dict(secret("gmail") or {})
    ways = list(g.get("match_by", ["guest_email", "title_name"]))
    out = []
    now_dt = datetime.now(TZ)
    for r in meetings.itertuples():
        found, how, amount, time_diff_str = None, "", 0.0, "—"
        if r.end <= now_dt and not emails.empty:
            c = emails[(emails["sender"] == str(r.email).lower()) & (emails["ts"] >= r.ts)]
            guests = {x.strip() for x in str(r.guests).split(",") if x.strip()}
            name = title_name_tokens(r.title)
            for e in c.sort_values("ts").itertuples():
                matched_by_rule = ""
                if "guest_email" in ways and guests & set(e.rcpt.split(",")):
                    matched_by_rule = "email khách"
                elif "title_name" in ways and name:
                    email_subj_tokens = set(tokens(e.subject))
                    common_tokens = name & email_subj_tokens
                    if common_tokens or (len(name) > 0 and len(common_tokens) / len(name) >= 0.4):
                        matched_by_rule = "tên tiêu đề (linh hoạt)"
                
                if matched_by_rule:
                    found, how, amount = e, matched_by_rule, e.est_amount
                    time_diff = e.ts - r.end
                    time_diff_str = fmt_time_delta(time_diff)
                    break
        out.append({
            "event_id": r.event_id, 
            "recap": found is not None,
            "recap_subject": found.subject if found is not None else "", 
            "recap_by": how, 
            "est_amount": amount,
            "recap_delay_str": time_diff_str,
            "recap_msg_id": found.message_id if found is not None else "",
            "recap_link": gmail_link(getattr(found, "rfc_id", ""), found.message_id) if found is not None else "",
        })
    return pd.DataFrame(out, columns=["event_id", "recap", "recap_subject", "recap_by", "est_amount", "recap_delay_str",
                                      "recap_msg_id", "recap_link"])


# ════════════════════════════════════════════════════════════════════════════
# NGUỒN 3 — VOICECLOUD
# ════════════════════════════════════════════════════════════════════════════
def _dig(obj, path: str):
    for part in [p for p in path.split(".") if p]:
        obj = obj.get(part, []) if isinstance(obj, dict) else []
    return obj


@st.cache_data(ttl=CACHE_TTL, show_spinner="Đang tải lịch sử cuộc gọi từ VoiceCloud…")
def load_calls(d_start: date, d_end: date) -> pd.DataFrame:
    c = dict(secret("voicecloud"))
    t0, t1 = to_dt_range(d_start, d_end)
    t1 -= timedelta(seconds=1)
    fmt = c.get("date_format", "%Y-%m-%d %H:%M:%S")
    headers = {c.get("auth_header", "Authorization"): c.get("auth_value", "")}
    page_size, page = int(c.get("page_size", 500)), int(c.get("first_page", 1))
    url = c["base_url"].rstrip("/") + "/" + c["endpoint"].lstrip("/")
    records: list[dict] = []
    for _ in range(int(c.get("max_pages", 200))):
        params = {
            c.get("param_from", "from"): t0.strftime(fmt), c.get("param_to", "to"): t1.strftime(fmt),
            c.get("param_page", "page"): page, c.get("param_page_size", "limit"): page_size,
            **dict(c.get("extra_params", {})),
        }
        if c.get("method", "GET").upper() == "POST":
            r = requests.post(url, json=params, headers=headers, timeout=60)
        else:
            r = requests.get(url, params=params, headers=headers, timeout=60)
        r.raise_for_status()
        batch = _dig(r.json(), c.get("records_path", "data"))
        if not isinstance(batch, list) or not batch:
            break
        records.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    if not records:
        return pd.DataFrame(columns=["ts", "vc_id", "duration"])
    df = pd.DataFrame(records)
    f_id, f_start, f_dur = (c.get("field_agent", "extension"), c.get("field_start", "start_time"),
                            c.get("field_duration", "duration"))
    for f in (f_id, f_start, f_dur):
        if f not in df.columns:
            raise KeyError(f"Không thấy trường '{f}' trong dữ liệu VoiceCloud.")
    if (f_dir := c.get("field_direction")) and (val := c.get("outbound_value")):
        df = df[df[f_dir].astype(str).str.lower() == str(val).lower()]
    ts = pd.to_datetime(df[f_start], errors="coerce")
    ts = ts.dt.tz_localize(TZ) if ts.dt.tz is None else ts.dt.tz_convert(TZ)
    return pd.DataFrame({
        "ts": ts, "vc_id": df[f_id].map(clean_id),
        "duration": pd.to_numeric(df[f_dur], errors="coerce").fillna(0),
    }).dropna(subset=["ts"])


# ════════════════════════════════════════════════════════════════════════════
# DỮ LIỆU DEMO
# ════════════════════════════════════════════════════════════════════════════
DEMO_SALES = ["Nguyễn Minh Anh", "Trần Quốc Bảo", "Lê Thu Hà", "Phạm Đức Huy",
              "Võ Ngọc Lan", "Đặng Hoàng Nam", "Bùi Khánh Linh", "Hoàng Gia Phúc"]
DEMO_CLIENTS = ["ABC Logistics", "Minh Phát", "Sao Việt Retail", "Hưng Thịnh Group", "Tân Á Foods",
                "Đại Dương Tech", "Phú Gia Pharma", "Kim Long Steel"]


def demo_roster() -> pd.DataFrame:
    n = len(DEMO_SALES)
    return pd.DataFrame({"sale": DEMO_SALES, "email": [f"sale{i}@demo.vn" for i in range(n)],
                         "bitrix_id": [str(100 + i) for i in range(n)], "vc_id": [str(1001 + i) for i in range(n)]})


def _demo_events(rng, weights: dict, days: list[date], per_day: float, hours=(8, 18)) -> pd.DataFrame:
    rows = []
    for d in days:
        if d.weekday() == 6:
            continue
        for sale, w in weights.items():
            for _ in range(rng.poisson(per_day * w)):
                rows.append({"ts": datetime.combine(d, time(int(rng.integers(*hours)), int(rng.integers(0, 60))), TZ),
                             "sale": sale})
    return pd.DataFrame(rows, columns=["ts", "sale"])


def _prev_month_start(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def _demo_lead_detail(today: date) -> pd.DataFrame:
    rows = []
    srcs = ["Website", "Facebook Ads", "Hotline", "Sale tự tìm", "Partner giới thiệu", "Sự kiện"]
    for s_ in DEMO_SALES:
        r = np.random.default_rng(sum(map(ord, s_)))
        for st_, n_ in zip(LEAD_STAGES_DEFAULT, r.poisson([12, 9, 7, 4, 5, 16])):
            for _ in range(int(n_)):
                rows.append({"sale": s_, "stage": st_, "source": srcs[int(r.choice(6, p=[.3, .25, .15, .12, .1, .08]))],
                             "created": today - timedelta(days=int(r.integers(0, 120))),
                             "reason": ([["A5. Invalid Phone", "A6. Không đăng ký", "A2. Sinh viên Test",
                                          "F4. Khách trả lời không có nhu cầu", "B8.2. Không nghe máy"][int(r.integers(0, 5))],
                                         ["Khách chỉ hỏi giá, chưa có ngân sách", "Đã dùng nền tảng khác",
                                          "Gọi 3 lần không bắt máy", ""][int(r.integers(0, 4))]]
                                        if st_ == "Unqualified" else ""), "n": 1,
                             "need": ["Tối ưu lại website đang có", "Làm website mới", "Chatbot tự động CSKH", "(Khác / không theo mẫu)",
                                      "Bán hàng đa kênh"][int(r.integers(0, 5))]})
    df = pd.DataFrame(rows, columns=LEAD_DETAIL_COLS)
    df = df[df["created"] >= _prev_month_start(today)]
    df["reason"] = df["reason"].map(lambda v: tuple(v) if isinstance(v, list) else v)
    out_ = df.groupby(["sale", "stage", "source", "created", "reason", "need"], as_index=False)["n"].sum()
    out_["reason"] = out_["reason"].map(lambda v: list(v) if isinstance(v, tuple) else v)
    return out_


def _demo_deal_lost(today: date) -> pd.DataFrame:
    rows = []
    srcs = ["Website", "Facebook Ads", "Hotline", "Sale tự tìm", "Partner giới thiệu", "Sự kiện"]
    reasons = ["L1. Giá cao", "L2. Chọn đối thủ", "L3. Chưa có nhu cầu", "L4. Không liên lạc được"]
    for s_ in DEMO_SALES:
        r = np.random.default_rng(sum(map(ord, s_)) + 7)
        for _ in range(int(r.poisson(5))):
            rows.append({"sale": s_, "source": srcs[int(r.integers(0, 6))],
                         "closed": today - timedelta(days=int(r.integers(0, (today - _prev_month_start(today)).days + 1))),
                         "reason": reasons[int(r.integers(0, 4))], "n": 1})
    df = pd.DataFrame(rows, columns=DEAL_LOST_COLS)
    return df.groupby(["sale", "source", "closed", "reason"], as_index=False)["n"].sum()


DEMO_COMMENTS = ["Đang liên hệ với khách\nKhách đang so sánh thêm phương án khác, chưa chọn bên nào",
                 "Khách đã gửi báo giá về công ty mẹ và đang đợi trình duyệt",
                 "Đã meeting\nKhách thấy báo giá hơi cao => đề xuất khách trao đổi nội bộ về ngân sách",
                 "Khách cần làm website bán hàng\nĐơn hàng web => admin => kho (đang hỏi thêm để check API)",
                 "Gọi lần đầu, hẹn demo tuần sau"]


def demo_data(d_start: date, d_end: date) -> dict:
    rng = np.random.default_rng(int(d_start.strftime("%Y%m%d")) + (d_end - d_start).days)
    today = datetime.now(TZ).date()
    days = [d for d in (d_start + timedelta(days=i) for i in range((d_end - d_start).days + 1)) if d <= today]
    w = dict(zip(DEMO_SALES, np.linspace(1.4, 0.6, len(DEMO_SALES))))
    meetings = _demo_events(rng, w, days, 1.4, hours=(9, 17))
    meetings["end"] = meetings["ts"] + pd.to_timedelta(rng.choice([30, 45, 60], len(meetings)), unit="min")
    client = rng.choice(DEMO_CLIENTS, len(meetings))
    meetings["title"] = [f"{k} – {c}" for k, c in zip(rng.choice(["Demo sản phẩm", "Gặp khách", "Báo giá"], len(meetings)), client)]
    slug = dict(zip(DEMO_CLIENTS, ["abclogistics", "minhphat", "saoviet", "hungthinh", "tanafoods",
                                   "daiduongtech", "phugia", "kimlong"]))
    meetings["guests"] = [f"lienhe@{slug[c]}.vn" for c in client]
    meetings["event_id"] = [f"demo{i}" for i in range(len(meetings))]
    
    p = meetings["sale"].map(dict(zip(DEMO_SALES, np.linspace(0.95, 0.45, len(DEMO_SALES)))))
    meetings["recap"] = rng.random(len(meetings)) < p
    meetings["recap_subject"] = np.where(meetings["recap"], "Haravan gửi recap – " + pd.Series(client).values, "")
    meetings["recap_by"] = np.where(meetings["recap"], "tên trong tiêu đề", "")
    meetings["est_amount"] = np.where(meetings["recap"], rng.integers(1, 20, len(meetings)) * 10000000.0, 0.0)
    
    delays = [fmt_time_delta(timedelta(minutes=int(m))) for m in rng.integers(15, 2800, len(meetings))]
    meetings["recap_delay_str"] = np.where(meetings["recap"], delays, "—")
    meetings["recap_msg_id"] = np.where(meetings["recap"], "demo_msg_id", "")
    meetings["recap_link"] = np.where(meetings["recap"], "https://mail.google.com/mail/u/0/#search/recap", "")

    calls = _demo_events(rng, w, days, 22)
    calls["duration"] = rng.gamma(2.0, 70, len(calls)).round()
    out = {
        "leads_new": _demo_events(rng, w, days, 2.2),
        "lead_stages": lead_stage_summary(ld := _demo_lead_detail(today)),
        "lead_detail": ld,
        "deal_lost": _demo_deal_lost(today),
        "deals_new": _demo_events(rng, w, days, 0.9),
        "deals_won": (dw := _demo_events(rng, w, days, 0.3)).assign(amount=rng.lognormal(17.6, 0.6, len(dw)).round(-5)),
        "meetings": meetings,
        "calls": calls,
    }
    dw = out["deals_won"]
    dn = out["deals_new"]
    out["new_deal_list"] = pd.DataFrame({"sale": dn["sale"].values, "ts": dn["ts"].values,
                                         "title": [f"Haravan x {c}" for c in rng.choice(DEMO_CLIENTS, len(dn))],
                                         "link": ""}, columns=NEW_DEAL_COLS)
    st_demo = [("SQL (Contact)", 10), ("Meeting Demo", 20), ("Negotiation", 30), ("Signed Contract", 40)]
    r_ = np.random.default_rng(7)
    out["deal_open"] = pd.DataFrame(
        [{"sale": s_, "stage_id": f"S{o}", "stage": nm, "cat": "Pipeline chính", "order": o,
          "created": (cd := today - timedelta(days=int(r_.integers(0, 120)))), "n": 1,
          "amount": float(r_.integers(0, 30)) * 1e6, "id": str(5000 + j_), "title": f"Haravan x {r_.choice(DEMO_CLIENTS)}",
          "created_ts": pd.Timestamp(datetime.combine(cd, time(9), TZ)), "link": "",
          "package": str(r_.choice(["STANDARD", "PRO", "BUSINESS WEB", ""])),
          "modified_ts": pd.Timestamp(datetime.combine(today - timedelta(days=int(r_.integers(0, 30))), time(15), TZ)),
          "comments": [[(datetime.combine(today - timedelta(days=int(k_ * 9 + r_.integers(0, 5))), time(15, 5), TZ)).isoformat(),
                        s_, t_] for k_, t_ in enumerate(DEMO_COMMENTS[int(r_.integers(0, 3)):])]}
         for j_, (s_, (nm, o)) in enumerate((s_, so) for s_ in DEMO_SALES for so in st_demo for _ in range(int(r_.poisson(4))))],
        columns=DEAL_OPEN_COLS)
    out["won_list"] = pd.DataFrame({"sale": dw["sale"].values, "id": [str(10000 + i) for i in range(len(dw))],
                                  "title": [f"Haravan x {c} – Website" for c in rng.choice(DEMO_CLIENTS, len(dw))],
                                  "amount": dw["amount"].values,
                                  "source": rng.choice(["Website", "Facebook Ads", "Hotline", "Chat", "Sale tự tìm",
                                                        "Partner giới thiệu"], len(dw)),
                                  "day": dw["ts"].dt.date.values, "link": "",
                                  "package": rng.choice(["STANDARD", "PRO", "BUSINESS WEB", "OMNI ADVANCED", "GROW"], len(dw))},
                                 columns=WON_LIST_COLS)
    rows_ = [{"id": r.id, "sale": r.sale, "title": r.title, "status": "open", "stage": r.stage, "amount": r.amount,
              "modified_ts": r.modified_ts, "closed": None, "lost_reason": "", "link": "", "comments": r.comments}
             for r in out["deal_open"].itertuples()]
    rows_ += [{"id": r.id, "sale": r.sale, "title": r.title, "status": "won", "stage": "Won", "amount": r.amount,
               "modified_ts": pd.NaT, "closed": r.day, "lost_reason": "", "link": ""} for r in out["won_list"].itertuples()]
    rows_ += [{"id": str(20000 + i), "sale": s_, "title": f"Haravan x {c}", "status": "lost", "stage": "Lost",
               "amount": 0.0, "modified_ts": pd.NaT, "closed": today, "lost_reason": "Giá cao", "link": ""}
              for i, (s_, c) in enumerate(zip(DEMO_SALES, DEMO_CLIENTS[::-1]))]
    out["deal_all"] = pd.DataFrame(rows_, columns=DEAL_ALL_COLS)
    return out
# ════════════════════════════════════════════════════════════════════════════
# SIDEBAR — BỘ LỌC & SYNC BITRIX
# ════════════════════════════════════════════════════════════════════════════
SOURCES = ["Danh sách Sale", "Bitrix24", "Google Calendar", "Gmail (recap)", "VoiceCloud"]
DEMO = not any(configured(s) for s in ("sales", "google_sheets", "bitrix24", "voicecloud", "google_calendar",
                                       "google_oauth"))

with st.sidebar:
    st.header("⚙️ Bộ lọc")
    mode_options = ["Hôm nay", "Hôm qua", "Tuần này", "Tháng này", "Tháng trước", "Quý này", "Tùy chọn khoảng ngày"]
    mode = st.selectbox("Mốc thời gian", mode_options, index=3)
    
    now = datetime.now(TZ)
    anchor = now.date()
    custom_range = None
    
    if mode == "Tùy chọn khoảng ngày":
        custom_range = st.date_input(
            "Chọn khoảng ngày (Từ ngày - Đến ngày)",
            value=(now.date() - timedelta(days=7), now.date()),
            max_value=now.date(),
            format="DD/MM/YYYY"
        )
    
    d_start, d_end = period_range(mode, anchor, custom_range)
    st.logo(_period_badge(d_start, d_end, now.date()), size="large")  # ô trên cùng thanh bên: tên kỳ đang xem
    st.caption(f"Kỳ báo cáo: **{d_start:%d/%m/%Y} → {d_end:%d/%m/%Y}**")
    sale_slot = st.container()

    if st.button("🔄 Làm mới dữ liệu", width="stretch",
                 help="Tải lại lịch, email và dữ liệu Bitrix/HaraCRM đã lưu (bỏ bộ nhớ đệm 10 phút)."):
        st.cache_data.clear()
        st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# TẢI DỮ LIỆU
# ════════════════════════════════════════════════════════════════════════════
status: dict[str, str] = {}
problems: list[str] = []
data = {
    "leads_new": pd.DataFrame(columns=["ts", "sale"]),
    "lead_stages": pd.DataFrame(columns=["sale", "stage", "n"]),
    "lead_detail": pd.DataFrame(columns=LEAD_DETAIL_COLS),
    "deal_lost": pd.DataFrame(columns=DEAL_LOST_COLS),
    "won_list": pd.DataFrame(columns=WON_LIST_COLS),
    "deal_open": pd.DataFrame(columns=DEAL_OPEN_COLS),
    "new_deal_list": pd.DataFrame(columns=NEW_DEAL_COLS),
    "deal_all": pd.DataFrame(columns=DEAL_ALL_COLS),
    "deals_new": pd.DataFrame(columns=["ts", "sale"]),
    "deals_won": pd.DataFrame(columns=["ts", "sale", "amount"]),
    "revenue": pd.DataFrame(columns=["ts", "sale", "amount"]),
    "meetings": pd.DataFrame(columns=["event_id", "sale", "email", "ts", "end", "title", "guests", "recap",
                                      "recap_subject", "recap_by", "est_amount", "recap_delay_str", "recap_msg_id",
                                      "recap_link"]),
    "calls": pd.DataFrame(columns=["ts", "sale", "duration"]),
}
raw_emails_df = pd.DataFrame(columns=["sender", "ts", "subject", "rcpt", "est_amount", "message_id", "rfc_id"])


def attempt(name: str, fn, *args):
    try:
        res = fn(*args)
        errs = getattr(res, "attrs", {}).get("errors", [])
        if not status.get(name, "").startswith(("❌", "⚠️")):
            status[name] = "⚠️ Lỗi một phần" if errs else "✅ Đã kết nối"
        problems.extend(f"**{name}** — {e}" for e in errs)
        return res
    except Exception as e:
        status[name] = "❌ Lỗi"
        problems.append(f"**{name}** — {type(e).__name__}: {e}")
        return None


if DEMO:
    roster = demo_roster()
    data.update(demo_data(d_start, d_end))
    status = {k: "🧪 Demo" for k in SOURCES}
    gmail_on = True
else:
    if configured("google_sheets"):
        roster = attempt("Danh sách Sale", load_roster_from_sheet)
    else:
        roster = attempt("Danh sách Sale", load_roster_from_secrets)
    if roster is None or roster.empty:
        roster = pd.DataFrame(columns=ROSTER_COLS)
        status["Danh sách Sale"] = "❌ Trống"

    id2sale = dict(zip(roster["bitrix_id"], roster["sale"]))
    vc2sale = dict(zip(roster["vc_id"], roster["sale"]))

    snap, snap_where, snap_err = (get_snapshot() if not roster.empty and bitrix_mode() == "bookmarklet"
                                  else (None, "", ""))
    sync_bx = None if snap else load_bitrix_from_sync_file()
    if snap:
        data.update(snapshot_frames(snap, id2sale, d_start, d_end))
        if "calls" in snap and not configured("voicecloud"):
            status["VoiceCloud"] = ("✅ Qua Bitrix24" + (" (tổng đài)" if snap.get("callSrc") == "telephony" else
                                                       " (hoạt động CRM)" if snap.get("callSrc") == "activity" else
                                                       " — chưa thấy cuộc gọi"))
        snap_at = datetime.fromtimestamp(snap["at"] / 1000, TZ)
        snap_old = now - snap_at > timedelta(hours=24)
        status["Bitrix24"] = f"{'⚠️' if snap_old else '✅'} Cập nhật {snap_at:%H:%M %d/%m}"
        if snap_old:
            problems.append(f"**Bitrix24** — dữ liệu cập nhật lần cuối lúc {snap_at:%H:%M %d/%m}. "
                            "Leader bấm nút **📥 Cập nhật Bitrix** trên Bitrix24 để lấy số mới.")
        if d_start < date.fromisoformat(snap.get("from", "1970-01-01")):
            problems.append(f"**Bitrix24** — dữ liệu chỉ có từ {snap['from']}; kỳ đang xem bắt đầu sớm hơn.")
    if snap_err and not snap:
        hint = ("Cần **bật Google Drive API** trong Google Cloud và leader vào **🔑 Kết nối Google** đăng nhập lại "
                "(tick đủ 3 quyền) để lưu dữ liệu Bitrix cho cả team."
                if any(k in snap_err for k in ("403", "insufficient", "accessNotConfigured", "has not been used",
                                               "invalid_scope", "SERVICE_DISABLED")) else snap_err)
        problems.append(f"**Bitrix24 (Google Drive)** — {hint}")
    if snap:
        pass  # đã nạp từ nút 📥 Cập nhật Bitrix ở trên
    elif sync_bx:
        status["Bitrix24"] = "✅ Đã sync (Extension/Drive)"
        for k in ("leads_new", "deals_new", "deals_won", "lead_stages"):
            df_k = sync_bx[k]
            if "bitrix_id" in df_k.columns:
                df_k["sale"] = df_k["bitrix_id"].map(id2sale)
            data[k] = df_k.dropna(subset=["sale"]) if "sale" in df_k.columns else df_k
    elif bitrix_mode() == "webhook" and not roster.empty:
        bx = attempt("Bitrix24", load_bitrix, tuple(roster["bitrix_id"]), d_start, d_end)
        if bx is not None:
            for k in ("leads_new", "deals_new", "deals_won", "lead_stages"):
                data[k] = bx[k].assign(sale=bx[k]["bitrix_id"].map(id2sale)).dropna(subset=["sale"])

    if not snap and not sync_bx and bitrix_mode() == "bookmarklet" and configured("bitrix24"):
        status.setdefault("Bitrix24", "⚪ Chưa cập nhật")

    g_leader = leader_mode()
    g_sa = configured("google_calendar") and configured("gcp_service_account")
    if (g_leader or g_sa) and not roster.empty:
        m = attempt("Google Calendar", load_meetings_leader if g_leader else load_meetings,
                    tuple(roster["email"]), tuple(roster["sale"]), d_start, d_end)
        if m is not None:
            data["meetings"] = m.assign(recap=pd.NA, est_amount=0.0, recap_delay_str="—", recap_msg_id="")

    gmail_on = g_leader or (configured("gmail") and configured("gcp_service_account"))
    mt = data["meetings"]
    if gmail_on and not roster.empty:
        senders = tuple(sorted(set(roster["email"])))
        raw_emails_df = (attempt("Gmail (recap)", load_recap_emails, senders, d_start, d_end, g_leader) if senders
                         else pd.DataFrame(columns=["sender", "ts", "subject", "rcpt", "est_amount", "message_id", "rfc_id"]))
        if raw_emails_df is not None and not mt.empty:
            rc = match_recaps(mt, raw_emails_df)
            data["meetings"] = mt.drop(columns=["recap", "est_amount", "recap_delay_str", "recap_msg_id", "recap_link"], errors="ignore").merge(rc, on="event_id", how="left")
            status.setdefault("Gmail (recap)", "✅ Đã kết nối")

    vc_snap = None
    if not roster.empty:
        try:
            vc_snap = get_snapshot(VC_SNAP)[0]
        except Exception as e:
            problems.append(f"**VoiceCloud** — {type(e).__name__}: {e}")
    if vc_snap and vc_snap.get("kind") == "vc":   # số cuộc gọi đi đọc từ portal.voicecloud.vn (nút 📞)
        vrows = []
        for sale_, days_ in (vc_snap.get("calls") or {}).items():
            for day_, (n_, _ok, talk_) in days_.items():
                d_ = date.fromisoformat(day_)
                if d_start <= d_ <= d_end and int(n_):
                    ts_ = datetime.combine(d_, time(12), TZ)
                    vrows += [{"ts": ts_, "sale": sale_, "duration": float(talk_) / int(n_)}] * int(n_)
        covered = set(vc_snap.get("sales") or []) | set((vc_snap.get("calls") or {}).keys())
        base_ = data["calls"]
        base_ = base_[~base_["sale"].isin(covered)] if not base_.empty else base_
        data["calls"] = pd.concat([base_, pd.DataFrame(vrows, columns=["ts", "sale", "duration"])], ignore_index=True)
        vc_at = datetime.fromtimestamp(vc_snap.get("at", 0) / 1000, TZ)
        status["VoiceCloud"] = (f"{'⚠️' if now - vc_at > timedelta(hours=24) else '✅'} Cập nhật {vc_at:%H:%M %d/%m} "
                                f"({str(vc_snap.get('from', ''))[:10]} → {str(vc_snap.get('to', ''))[:10]})")
        if str(vc_snap.get("from", ""))[:10] > f"{d_start:%Y-%m-%d}":
            problems.append(f"**VoiceCloud** — dữ liệu cuộc gọi chỉ có từ {str(vc_snap.get('from'))[:10]}; "
                            "bấm lại nút 📞 Cập nhật VoiceCloud để lấy đủ kỳ đang xem.")
    if configured("voicecloud") and not roster.empty:
        calls = attempt("VoiceCloud", load_calls, d_start, d_end)
        if calls is not None:
            data["calls"] = calls.assign(sale=calls["vc_id"].map(vc2sale)).dropna(subset=["sale"])

    for s in SOURCES:
        status.setdefault(s, "⚪ Chưa cấu hình")

with sale_slot:
    picked = st.multiselect("👤 Lọc theo Sale", roster["sale"].tolist(), placeholder="Tất cả Sale",
                            help="Chọn một hoặc vài Sale. Để trống = cả team.")
OWNER, OWNER_CFG = owner_mode()
with st.sidebar:
    st.divider()
    if OWNER:
        st.subheader("Trạng thái kết nối")
        for k in SOURCES:
            st.markdown(f"{status[k]} &nbsp; {k}")
        st.caption(f"Dữ liệu cache {CACHE_TTL // 60} phút · Cập nhật: {now:%H:%M %d/%m}")
        if not OWNER_CFG and not DEMO:
            st.caption("🔓 Ai mở dashboard cũng thấy mục Cài đặt. Thêm `admin_key` vào mục `[app]` trong Secrets "
                       "để chỉ chủ dashboard thấy.")
    else:
        st.caption(f"Cập nhật: {now:%H:%M %d/%m}")
        if secret("app", "admin_key", ""):
            with st.expander("🔒 Quản trị"):
                with st.form("owner_login", border=False):
                    k_ = st.text_input("Mã quản trị", type="password")
                    if st.form_submit_button("Mở"):
                        if hmac.compare_digest(k_.encode(), str(secret("app", "admin_key", "")).encode()):
                            st.session_state["is_owner"] = True
                            st.rerun()
                        st.error("Sai mã.")

if roster.empty:
    st.title("📊 Dashboard Hiệu suất Sale")
    st.error("Chưa có danh sách Sale. Khai báo các mục `[[sales]]` trong Secrets.")
    for p in problems:
        st.markdown(f"- {p}")
    st.stop()

if data["revenue"].empty and not globals().get("snap"):   # webhook / demo: doanh thu theo deal won
    data["revenue"] = data["deals_won"][["ts", "sale", "amount"]]
try:
    ADJ = load_adjustments()
except Exception as e:
    ADJ = pd.DataFrame(columns=ADJ_COLS)
    problems.append(f"**Điều chỉnh doanh thu** — {type(e).__name__}: {e}")
OVR = load_overrides()
_ovr_rows = []
if OVR and not data["won_list"].empty:   # sửa số tiền deal won Bitrix → chênh lệch cộng vào doanh thu
    _wl0 = data["won_list"].copy()
    _wl0["bitrix_amount"] = _wl0["amount"]
    for i_, r_ in _wl0.iterrows():
        o_ = OVR.get(str(r_["id"]))
        if o_ is None:
            continue
        new_ = float(o_["amount"])
        if new_ != float(r_["amount"]):
            _ovr_rows.append({"day": r_["day"], "sale": r_["sale"], "amount": new_ - float(r_["amount"]), "deals": 0,
                              "source": "", "package": "", "note": f"Sửa số tiền deal {r_['id']}"})
        _wl0.at[i_, "amount"] = new_
        _wl0.at[i_, "fixed_note"] = str(o_.get("note") or "")
    data["won_list"] = _wl0
    if _ovr_rows:
        _od = pd.DataFrame(_ovr_rows)
        _rv0 = pd.DataFrame({"ts": (pd.to_datetime(_od["day"]).dt.tz_localize(TZ) + pd.Timedelta(hours=12)).values,
                             "sale": _od["sale"].values, "amount": _od["amount"].values})
        data["revenue"] = _rv0 if data["revenue"].empty else pd.concat([data["revenue"], _rv0], ignore_index=True)
ADJ_IN = ADJ[(ADJ["day"] >= d_start) & (ADJ["day"] <= d_end)] if not ADJ.empty else ADJ
ADJ_ALL = pd.concat([ADJ_IN, pd.DataFrame(_ovr_rows, columns=ADJ_COLS)], ignore_index=True) if _ovr_rows else ADJ_IN
if not ADJ_IN.empty:   # cộng điều chỉnh thủ công vào doanh thu + danh sách deal won
    _ts = pd.to_datetime(ADJ_IN["day"]).dt.tz_localize(TZ) + pd.Timedelta(hours=12)
    _rv = pd.DataFrame({"ts": _ts.values, "sale": ADJ_IN["sale"].values, "amount": ADJ_IN["amount"].values})
    data["revenue"] = _rv if data["revenue"].empty else pd.concat([data["revenue"], _rv], ignore_index=True)
    _wl = pd.DataFrame({
        "sale": ADJ_IN["sale"].values, "id": "", "amount": ADJ_IN["amount"].values,
        "title": ["✏️ " + (n or "Deal won thêm tay") for n in ADJ_IN["note"]],
        "source": ADJ_IN["source"].replace("", "(Điều chỉnh thủ công)").values, "day": ADJ_IN["day"].values,
        "link": "", "package": ADJ_IN["package"].replace("", "(Chưa chọn gói)").values,
        "pu": ADJ_IN["deals"].values})
    data["won_list"] = _wl if data["won_list"].empty else pd.concat([data["won_list"], _wl], ignore_index=True)
sales_in_view = picked or roster["sale"].tolist()
for k in data:
    if "sale" in data[k].columns:
        data[k] = data[k][data[k]["sale"].isin(sales_in_view)]


# ════════════════════════════════════════════════════════════════════════════
# TRẠNG THÁI RECAP CỦA TỪNG MEETING
# ════════════════════════════════════════════════════════════════════════════
mt = data["meetings"].copy()


def recap_status(r) -> str:
    if r["end"] > now:
        return ST_UPCOMING
    if not gmail_on or pd.isna(r["recap"]):
        return ST_UNKNOWN
    if bool(r["recap"]):
        return ST_DONE
    return ST_MISS


mt["status"] = mt.apply(recap_status, axis=1) if not mt.empty else pd.Series(dtype=str)


# ════════════════════════════════════════════════════════════════════════════
# TỔNG HỢP THEO SALE
# ════════════════════════════════════════════════════════════════════════════
def count(df: pd.DataFrame, name: str) -> pd.Series:
    return df.groupby("sale").size().rename(name)


st_counts = (mt.groupby(["sale", "status"]).size().unstack(fill_value=0)
             if not mt.empty else pd.DataFrame())
for s in STATUS_COLORS:
    if s not in st_counts.columns:
        st_counts[s] = 0

stages, closed_stages = lead_stage_config()
# Stage không tính vào "Lead tồn": Unqualified (đã loại) + Qualified (đã chuyển thành deal)
not_open_stages = list(secret("bitrix24", "lead_not_open_stages", closed_stages + ["Qualified"]))
closed_l = [c.lower() for c in not_open_stages]
open_stages = [s_ for s_ in stages if s_.lower() not in closed_l]
stage_tbl = (data["lead_stages"].pivot_table(index="sale", columns="stage", values="n", aggfunc="sum")
             .reindex(index=sales_in_view, columns=stages).fillna(0).astype(int))
stage_tbl["lead_open"] = stage_tbl[open_stages].sum(axis=1)

lb = (
    pd.DataFrame({"sale": sales_in_view})
    .merge(count(data["leads_new"], "leads_new"), on="sale", how="left")
    .merge(stage_tbl["lead_open"], left_on="sale", right_index=True, how="left")
    .merge(count(data["deals_new"], "deals_new"), on="sale", how="left")
    .merge(count(data["deals_won"], "deals_won"), on="sale", how="left")
    .merge(data["revenue"].groupby("sale")["amount"].sum().rename("revenue"), on="sale", how="left")
    .merge(data["calls"].groupby("sale").agg(calls=("duration", "size"), talk=("duration", "sum")), on="sale", how="left")
    .merge(count(mt, "meetings"), on="sale", how="left")
    .merge(mt.groupby("sale")["est_amount"].sum().rename("est_revenue"), on="sale", how="left")
    .merge(st_counts[[ST_DONE, ST_MISS]].rename(columns={ST_DONE: "recap_done", ST_MISS: "recap_miss"}),
           left_on="sale", right_index=True, how="left")
    .fillna(0)
)
_adj_g = ADJ_ALL.groupby("sale") if not ADJ_ALL.empty else None
lb["adj_amount"] = lb["sale"].map(_adj_g["amount"].sum()).fillna(0.0) if _adj_g is not None else 0.0
lb["deals_won"] = (lb["deals_won"] + (lb["sale"].map(_adj_g["deals"].sum()).fillna(0) if _adj_g is not None else 0)
                   ).clip(lower=0)
int_cols = ["leads_new", "lead_open", "deals_new", "deals_won", "calls", "meetings", "recap_done", "recap_miss"]
lb[int_cols] = lb[int_cols].astype(int)
held = lb["recap_done"] + lb["recap_miss"]
lb["recap_rate"] = lb["recap_done"] / held.replace(0, np.nan)
lb = lb.sort_values(["revenue", "deals_won", "deals_new"], ascending=False).reset_index(drop=True)


# ════════════════════════════════════════════════════════════════════════════
# CÁC TRANG
# ════════════════════════════════════════════════════════════════════════════
def trend(df: pd.DataFrame) -> pd.DataFrame:
    t0, t1 = to_dt_range(d_start, d_end)
    freq = "h" if d_start == d_end else "D"
    idx = pd.date_range(t0, t1, freq=freq, inclusive="left")
    if d_start == d_end:
        idx = idx[(idx.hour >= 7) & (idx.hour <= 20)]
    idx = idx[idx <= pd.Timestamp(now).floor(freq)]
    if df.empty:
        return pd.DataFrame({"ts": idx, "n": 0})
    s = pd.to_datetime(df["ts"]).dt.tz_convert(TZ).dt.floor(freq).value_counts().reindex(idx, fill_value=0)
    return pd.DataFrame({"ts": s.index, "n": s.values})


def trend_chart(df: pd.DataFrame, color: str, label: str):
    tickfmt, hoverfmt = ("%H:%M", "%H:%M") if d_start == d_end else ("%d/%m", "%a %d/%m")
    t0, _ = to_dt_range(d_start, d_end)
    if d_start == d_end:
        x_range, dtick = [t0 + timedelta(hours=6, minutes=30), t0 + timedelta(hours=20, minutes=30)], 2 * 3600e3
    else:
        x_range = [t0 - timedelta(hours=12), datetime.combine(d_end, time(12), TZ)]
        days_diff = (d_end - d_start).days
        dtick = 86400e3 if days_diff <= 7 else (3 * 86400e3 if days_diff <= 31 else 7 * 86400e3)
    fig = px.line(df, x="ts", y="n", markers=True, labels={"ts": "", "n": label})
    fig.update_traces(line=dict(color=color, width=2), marker=dict(size=8),
                      hovertemplate=f"%{{x|{hoverfmt}}}<br>{label}: <b>%{{y}}</b><extra></extra>")
    fig.update_layout(height=300, margin=dict(l=0, r=10, t=10, b=0), hovermode="x unified",
                      xaxis=dict(tickformat=tickfmt, showgrid=False, range=x_range, dtick=dtick),
                      yaxis=dict(rangemode="tozero", showgrid=True))
    return fig


KPI_DEFAULT = {"Võ Minh Hiếu": 100_000_000, "Nguyễn Đức Toàn": 100_000_000,
               "Nguyễn Thị Tuyết Nhi": 100_000_000, "Lê Thị Ngọc Châu": 50_000_000}


def _plain(t) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", str(t).lower().replace("đ", "d"))
    return " ".join("".join(c for c in t if unicodedata.category(c) != "Mn").split())


def kpi_month(sale: str):
    """KPI doanh thu/tháng của Sale: lấy từ mục [kpi] trong Secrets (tên đầy đủ hoặc tên gọi), mặc định theo KPI_DEFAULT."""
    conf = dict(secret("kpi") or {}) or KPI_DEFAULT
    full = _plain(sale)
    for k, v in conf.items():
        kk = _plain(k)
        if kk == full or (kk and " " not in kk and full.split()[-1:] == [kk]):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def page_overview():
    tot_held = int(held.sum())
    rate = lb["recap_done"].sum() / tot_held if tot_held else 0
    row1, row2 = st.columns(5), st.columns(5)
    row1[0].metric("💰 Doanh thu", fmt_vnd(lb["revenue"].sum()),
                   help=(f"Tổng giá trị deal Won có **{(globals().get('snap') or {}).get('revField', [0, 'ngày ghi nhận doanh số'])[1]}** "
                         f"trong kỳ: {lb['revenue'].sum():,.0f} ₫".replace(",", ".")
                         if (globals().get("snap") or {}).get("revField")
                         else f"Tổng giá trị deal Won có ngày đóng trong kỳ: {lb['revenue'].sum():,.0f} ₫".replace(",", ".")))
    _rf = (globals().get("snap") or {}).get("revField")
    row1[1].metric("🏆 Deal won", fmt_int(lb["deals_won"].sum()),
                   help=f"Deal Won có **{_rf[1]}** trong kỳ" if _rf else "Deal chuyển sang trạng thái Won trong kỳ")
    _won, _rev = int(lb["deals_won"].sum()), float(lb["revenue"].sum())
    row1[2].metric("💎 APU toàn team", fmt_vnd(_rev / _won) if _won else "—",
                   help=f"Tổng doanh thu ÷ tổng deal won của team trong kỳ: {_rev:,.0f} ₫ ÷ {_won} deal".replace(",", "."))
    row1[3].metric("🤝 Deal mới", fmt_int(lb["deals_new"].sum()), help="Deal tạo mới trong kỳ")
    row1[4].metric("📥 Lead mới", fmt_int(lb["leads_new"].sum()), help="Lead tạo mới trong kỳ")
    row2[0].metric("📊 Doanh thu Est (Meeting)", fmt_vnd(lb["est_revenue"].sum()),
                   help="Tổng giá trị dự kiến trích xuất từ báo giá đính kèm/trong email recap.")
    row2[1].metric("📞 Cuộc gọi", fmt_int(lb["calls"].sum()), help=f"Tổng thời lượng: {fmt_duration(lb['talk'].sum())}")
    row2[2].metric("📅 Meeting", fmt_int(lb["meetings"].sum()), help="Meeting có khách ngoài công ty")
    row2[3].metric("✉️ Tỷ lệ recap", f"{rate:.0%}" if tot_held else "—",
                    help=f"Đã recap / meeting đã diễn ra. Thiếu recap: {fmt_int(lb['recap_miss'].sum())}")

    st.divider()
    st.markdown("**🏆 Bảng hiệu suất từng Sale**")
    n_months = (d_end.year * 12 + d_end.month) - (d_start.year * 12 + d_start.month) + 1
    kpi = lb["sale"].map(kpi_month).astype(float) * n_months
    view = pd.DataFrame({
        "Sale": lb["sale"],
        "KPI (₫)": kpi,
        "Doanh thu (₫)": lb["revenue"],
        "% KPI": (lb["revenue"] / kpi.replace(0, np.nan) * 100).round(1),
        "Est Doanh thu": lb["est_revenue"],
        "Lead mới": lb["leads_new"], "Lead tồn": lb["lead_open"],
        "Deal mới": lb["deals_new"], "Deal won": lb["deals_won"],
        "APU (₫)": (lb["revenue"] / lb["deals_won"].replace(0, np.nan)).fillna(0).round(0),
        "Cuộc gọi": lb["calls"], "Thời lượng gọi": lb["talk"].map(fmt_duration),
        "Meeting": lb["meetings"], "Đã recap": lb["recap_done"], "Thiếu recap": lb["recap_miss"],
        "Tỷ lệ recap": lb["recap_rate"].map(lambda v: "—" if pd.isna(v) else f"{v:.0%}"),
    })
    if len(view) > 1:   # dòng TỔNG toàn team
        tot = {c: view[c].sum() for c in ["Doanh thu (₫)", "Est Doanh thu", "Lead mới", "Lead tồn", "Deal mới",
                                          "Deal won", "Cuộc gọi", "Meeting", "Đã recap", "Thiếu recap"]}
        k_sum = kpi.sum(min_count=1)
        held_t = tot["Đã recap"] + tot["Thiếu recap"]
        tot.update({"Sale": "TỔNG", "KPI (₫)": k_sum,
                    "% KPI": round(tot["Doanh thu (₫)"] / k_sum * 100, 1) if k_sum and not pd.isna(k_sum) else np.nan,
                    "APU (₫)": round(tot["Doanh thu (₫)"] / tot["Deal won"]) if tot["Deal won"] else 0,
                    "Thời lượng gọi": fmt_duration(lb["talk"].sum()),
                    "Tỷ lệ recap": f"{tot['Đã recap'] / held_t:.0%}" if held_t else "—"})
        kpi = pd.concat([kpi, pd.Series([k_sum])], ignore_index=True)
        view = pd.concat([view, pd.DataFrame([tot])[view.columns]], ignore_index=True)
    view["KPI (₫)"] = kpi.map(lambda v: "" if pd.isna(v) else f"{v:,.0f}".replace(",", "."))
    money = ["Doanh thu (₫)", "Est Doanh thu", "APU (₫)"]
    st.dataframe(view.style
                 .format(lambda v: "" if pd.isna(v) else f"{v:,.0f}".replace(",", "."), subset=money)
                 .set_properties(subset=["Doanh thu (₫)"],
                                 **{"background-color": "rgba(255, 193, 7, 0.22)", "font-weight": "bold"})
                 .apply(lambda r: ["font-weight: bold; background-color: rgba(62, 180, 137, 0.18)"] * len(r)
                        if r["Sale"] == "TỔNG" else [""] * len(r), axis=1),
                 hide_index=True, width="stretch", column_config={
        "Sale": st.column_config.TextColumn(pinned=True),
        "Doanh thu (₫)": st.column_config.NumberColumn(
            help="Đã gồm phần sửa tay ở trang 💰 Danh sách deal won (sửa số tiền deal / deal ngoài Bitrix)"),
        "KPI (₫)": st.column_config.TextColumn(
                                                 help=f"KPI doanh thu/tháng × {n_months} tháng trong kỳ" if n_months > 1
                                                 else "KPI doanh thu của tháng"),
        "APU (₫)": st.column_config.NumberColumn(help="Doanh thu ÷ số deal won"),
        "% KPI": st.column_config.ProgressColumn("% KPI", format="%.0f%%", min_value=0, max_value=100,
                                                 help="Doanh thu ÷ KPI (loading KPI)"),
        "Est Doanh thu": st.column_config.NumberColumn(),
        "Thời lượng gọi": st.column_config.TextColumn(),
        "Thiếu recap": st.column_config.NumberColumn(),
        "Tỷ lệ recap": st.column_config.TextColumn(),
    })
    st.download_button("⬇️ Tải bảng (CSV)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"hieu_suat_sale_{d_start:%Y%m%d}_{d_end:%Y%m%d}.csv", mime="text/csv")
    if not ADJ_ALL.empty:
        st.caption(f"✏️ Doanh thu đã gồm **{len(ADJ_ALL)} điều chỉnh thủ công** trong kỳ "
                   f"(tổng {'+' if ADJ_ALL['amount'].sum() >= 0 else ''}{fmt_vnd(ADJ_ALL['amount'].sum())}"
                   f"{', ' + format(int(ADJ_ALL['deals'].sum()), '+d') + ' deal won' if ADJ_ALL['deals'].sum() else ''}) "
                   "— xem/sửa ở trang **💰 Danh sách deal won**.")
    else:
        st.caption("✏️ Sửa số tiền deal won hoặc thêm doanh thu ngoài Bitrix ở trang **💰 Danh sách deal won**.")
    _render_won_breakdown()


def _owner_unlock(key: str) -> bool:
    """True nếu là chủ dashboard; nếu chưa thì hiện ô nhập mã quản trị để mở khoá."""
    if OWNER:
        return True
    st.caption("Chỉ chủ dashboard được sửa. Nhập mã quản trị (`admin_key` trong Secrets) để mở.")
    with st.form(f"unlock_{key}", border=False):
        k_ = st.text_input("Mã quản trị", type="password", key=f"unlock_pw_{key}")
        if st.form_submit_button("🔓 Mở khoá"):
            key_ = str(secret("app", "admin_key", "") or "")
            if key_ and hmac.compare_digest(k_.encode(), key_.encode()):
                st.session_state["is_owner"] = True
                st.rerun()
            st.error("Sai mã." if key_ else "Chưa đặt `admin_key` trong mục `[app]` của Secrets.")
    return False


def _saved(where: str, err: str, editor_key: str):
    st.session_state["_adj_msg"] = (f"⚠️ Đã lưu tạm ({where}) — chưa lưu được lên Google Drive: {err}"
                                    if err and where != "Google Drive" else "✅ Đã lưu.")
    st.session_state.pop(editor_key, None)
    st.rerun()


def _render_won_edit(w_bitrix: pd.DataFrame):
    """Trang Danh sách deal won: sửa số tiền deal Bitrix + thêm deal won ngoài Bitrix."""
    with st.expander("✏️ Sửa số tiền / thêm deal won ngoài Bitrix",
                     expanded=bool(st.session_state.get("_adj_msg"))):
        if not _owner_unlock("won"):
            return
        msg_ = st.session_state.pop("_adj_msg", "")
        if msg_:
            (st.warning if msg_.startswith("⚠️") else st.success)(msg_)
        t1, t2 = st.tabs(["✏️ Sửa số tiền deal Bitrix", "➕ Thêm deal won ngoài Bitrix"])
        with t1:
            st.caption("Nhập **Số tiền đúng** cho deal bị lệch (để trống = dùng số Bitrix). Doanh thu ở bảng "
                       "Hiệu suất theo Sale tự cộng phần chênh lệch. **Nhập sai?** Bấm đúp vào ô để sửa lại số, hoặc "
                       "xoá trống ô để trả về số Bitrix, rồi bấm **💾 Lưu số tiền**. Deal đã sửa nằm trên cùng.")
            if w_bitrix.empty:
                st.info("Không có deal won Bitrix nào với bộ lọc hiện tại.")
            else:
                ov = dict(OVR)
                src = w_bitrix.assign(_e=w_bitrix["id"].astype(str).isin(ov.keys()))
                n_e = int(src["_e"].sum())
                if n_e and st.toggle(f"Chỉ hiện {n_e} deal đã sửa", key="ovr_only_edited"):
                    src = src[src["_e"]]
                src = src.sort_values(["_e", "day", "bitrix_amount"], ascending=[False, False, False])
                tbl = pd.DataFrame({
                    "ID": src["id"].astype(str).values, "Ngày": src["day"].values, "Sale": src["sale"].values,
                    "Tên deal": src["title"].values, "Bitrix ghi (₫)": src["bitrix_amount"].values,
                    "Số tiền đúng (₫)": [ov.get(str(i), {}).get("amount") for i in src["id"]],
                    "Ghi chú": [ov.get(str(i), {}).get("note", "") for i in src["id"]],
                })
                tbl["Số tiền đúng (₫)"] = pd.to_numeric(tbl["Số tiền đúng (₫)"], errors="coerce")
                ed = st.data_editor(tbl, hide_index=True, width="stretch", key="ovr_editor",
                                    disabled=["ID", "Ngày", "Sale", "Tên deal", "Bitrix ghi (₫)"],
                                    column_config={
                                        "ID": st.column_config.TextColumn(width="small"),
                                        "Ngày": st.column_config.DateColumn(format="DD/MM/YYYY"),
                                        "Tên deal": st.column_config.TextColumn(width="large"),
                                        "Bitrix ghi (₫)": st.column_config.NumberColumn(format="localized"),
                                        "Số tiền đúng (₫)": st.column_config.NumberColumn(format="localized", step=1000,
                                                                                         min_value=0),
                                        "Ghi chú": st.column_config.TextColumn(width="medium"),
                                    })
                if st.button("💾 Lưu số tiền", type="primary", key="ovr_save"):
                    for r in ed.itertuples(index=False):
                        did, amt_, note_ = str(r[0]), r[5], r[6]
                        if amt_ is None or pd.isna(amt_):
                            ov.pop(did, None)
                        else:
                            ov[did] = {"amount": float(amt_), "note": "" if note_ is None or pd.isna(note_) else str(note_)}
                    _saved(*save_adjustments(overrides=ov), "ovr_editor")
        with t2:
            st.caption("Doanh thu không có trên Bitrix (hoặc bù lệch chung). Tính vào tháng của cột **Ngày**. "
                       "Thêm dòng ở cuối bảng. **Nhập sai?** Bấm đúp vào ô để sửa; xoá dòng: tick ô đầu dòng rồi bấm 🗑. "
                       "Xong bấm **💾 Lưu deal thêm tay**.")
            cur = ADJ.sort_values("day", ascending=False).reset_index(drop=True)
            cur["source"] = cur["source"].where(cur["source"].isin(ADJ_SOURCES), None)
            ed2 = st.data_editor(
                cur.rename(columns={"day": "Ngày", "sale": "Sale", "amount": "Số tiền (₫)", "deals": "Số deal won",
                                    "source": "Nguồn", "package": "Gói dịch vụ", "note": "Tên deal / ghi chú"}),
                num_rows="dynamic", hide_index=True, width="stretch", key="adj_editor",
                column_config={
                    "Ngày": st.column_config.DateColumn(format="DD/MM/YYYY", default=now.date(), required=True),
                    "Sale": st.column_config.SelectboxColumn(options=list(roster["sale"]), required=True, width="medium"),
                    "Số tiền (₫)": st.column_config.NumberColumn(format="localized", step=1000, default=0,
                                                                 help="Số âm để trừ bớt"),
                    "Số deal won": st.column_config.NumberColumn(step=1, default=1, min_value=-50, max_value=50,
                                                                 help="Cộng vào số deal won (0 nếu chỉ bù tiền)"),
                    "Nguồn": st.column_config.SelectboxColumn(options=ADJ_SOURCES,
                                                              help="Để chia vào bảng doanh thu theo Source"),
                    "Gói dịch vụ": st.column_config.TextColumn(),
                    "Tên deal / ghi chú": st.column_config.TextColumn(width="large"),
                })
            if st.button("💾 Lưu deal thêm tay", type="primary", key="adj_save"):
                rows = []
                for r in ed2.itertuples(index=False):
                    day_, sale_, amt_, deals_ = r[0], r[1], r[2], r[3]
                    if day_ is None or pd.isna(day_) or not sale_:
                        continue
                    amt_ = float(0 if amt_ is None or pd.isna(amt_) else amt_)
                    deals_ = int(0 if deals_ is None or pd.isna(deals_) else deals_)
                    if not amt_ and not deals_:
                        continue
                    txt = lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()
                    rows.append({"day": pd.Timestamp(day_).date().isoformat(), "sale": str(sale_), "amount": amt_,
                                 "deals": deals_, "source": txt(r[4]), "package": txt(r[5]), "note": txt(r[6])})
                _saved(*save_adjustments(rows=rows), "adj_editor")


_HARA_NUM = re.compile(r"^(-?[\d.,]+\s*%?|—|-)\s+(.+)$")


def _hara_split(txt: str) -> tuple[str, str]:
    """'26%\n6/23' hoặc '26% 6/23' → ('26%', '6/23'); chữ thường giữ nguyên."""
    txt = str(txt or "").strip()
    if "\n" in txt:
        main, sub = txt.split("\n", 1)
        return main.strip(), " ".join(sub.split())
    m = _HARA_NUM.match(txt)
    return (m.group(1), m.group(2)) if m else (txt, "")


HARA_CSS = """<style>
.ht{border-collapse:collapse;width:100%;font-size:13.5px;font-family:inherit}
.ht th,.ht td{padding:6px 7px;border-bottom:1px solid rgba(128,128,128,.2);vertical-align:middle}
.ht th{font-weight:700;text-align:right;background:rgba(128,128,128,.06);line-height:1.25;min-width:52px}
.ht th.l,.ht td.l{text-align:left} .ht td{text-align:right;white-space:nowrap} .ht td.l{white-space:normal;min-width:125px}
.ht th .s{display:block;font-weight:400;font-size:11.5px;color:rgba(110,110,110,.95)}
.ht th.stg{background:rgba(59,130,246,.10)} .ht th.rt{background:rgba(250,204,21,.16)}
.ht th.win{background:rgba(255,193,7,.30)} .ht td.win{background:rgba(255,193,7,.16);font-weight:700}
.ht td .s{font-size:11.5px;color:rgba(120,120,120,.95);margin-left:4px}
.ht td .lo{font-style:italic}
.ht td.bad{background:rgba(239,68,68,.14);color:rgb(185,28,28);font-weight:700}
.ht td.bad .s{color:rgb(185,28,28)}
.ht tr.tb td{color:rgba(100,100,100,.95);background:rgba(128,128,128,.05)}
.ht tr.sum td{font-weight:700;background:rgba(62,180,137,.12);border-top:2px solid rgba(62,180,137,.45)}
.ht tr:hover td{background-color:rgba(62,180,137,.07)}
</style>"""


def hara_table_html(t: dict) -> str:
    """Bảng HaraCRM giữ format gốc: tiêu đề 2 dòng, số phụ (6/23, ít mẫu), ô đỏ, dòng TB team / Tổng."""
    headers = [_hara_split(h) if "\n" in str(h) else (str(h or ""), "") for h in (t.get("headers") or [])]
    rows = [list(r) for r in t.get("rows") or []]
    flags = t.get("flags") or [[0] * len(r) for r in rows]
    width = max([len(headers)] + [len(r) for r in rows] + [0])
    headers += [("", "")] * (width - len(headers))
    text_cols = set()
    for j in range(width):   # cột chữ (NV, Team…) canh trái
        vals = [r[j] for r in rows if j < len(r) and str(r[j]).strip()]
        if vals and sum(bool(re.match(r"^-?[\d.,]+\s*%?$|^—$", _hara_split(v)[0])) for v in vals) < len(vals) / 2:
            text_cols.add(j)
    def hcls(j):
        main, sub = headers[j]
        p_, ps = _plain(main).replace(" ", ""), _plain(sub)
        c = ["l"] if j in text_cols else []
        if p_ in ("lead→win", "lead->win"):
            c.append("win")
        elif "ty le" in ps:
            c.append("rt")
        elif "stage" in ps:
            c.append("stg")
        return " ".join(c)
    hc = [hcls(j) for j in range(width)]
    head = "".join(f'<th class="{hc[j]}">{escape(m).replace("→", "→<wbr>")}' + (f'<span class="s">{escape(sb)}</span>' if sb else "") + "</th>"
                   for j, (m, sb) in enumerate(headers))
    body = []
    for r, fl in zip(rows, flags):
        r = r + [""] * (width - len(r))
        first = _plain(" ".join(str(x) for x in r[:2]))
        rc = "sum" if first.startswith("tong") else ("tb" if first.startswith(("tb ", "trung binh")) else "")
        tds = []
        for j, v in enumerate(r):
            main, sub = _hara_split(v)
            cl = [c for c in hc[j].split() if c in ("l", "win")]
            if j < len(fl) and fl[j]:
                cl.append("bad")
            sub_h = (f'<span class="s{" lo" if "mau" in _plain(sub) else ""}">{escape(sub)}</span>' if sub else "")
            tds.append(f'<td class="{" ".join(cl)}">{escape(main)}{sub_h}</td>')
        body.append(f'<tr class="{rc}">' + "".join(tds) + "</tr>")
    return (f'{HARA_CSS}<div style="overflow-x:auto"><table class="ht"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def page_hara():
    if DEMO:
        st.info("Trang này hiện số lấy từ HaraCRM — chưa có ở chế độ demo.")
        return
    team = secret("haracrm", "team", "S2.4")
    h, where, err = get_snapshot(HARA_SNAP)
    if not h:
        st.info("Chưa có dữ liệu HaraCRM. Leader vào **⚙️ Cài đặt → 📈 Cập nhật HaraCRM** để cài nút, rồi bấm nút "
                "khi đang mở dashboard HaraCRM.")
        if err:
            st.caption(f"Google Drive: {err}")
        return
    at_ = datetime.fromtimestamp(h.get("at", 0) / 1000, TZ)
    periods = h.get("periods") or [{"label": h.get("filter") or "Kỳ đã chọn", "filter": h.get("filter"),
                                    "tables": h.get("tables", [])}]
    labels = [p_["label"] for p_ in periods]
    side = period_label(d_start, d_end, now.date())
    pick = st.segmented_control("Tháng (HaraCRM)", labels, default=side if side in labels else labels[-1],
                                key="hara_period", label_visibility="collapsed") or labels[-1]
    per = next(p_ for p_ in periods if p_["label"] == pick)
    filt_ = re.sub(r"\s*Đổi bộ lọc\s*", " ", str(per.get("filter") or "")).strip()
    st.caption(f"Kỳ: **{filt_ or pick}** · cập nhật {at_:%H:%M %d/%m}.")
    if per.get("err"):
        st.warning(f"Lần cập nhật này không lấy được {pick}: {per['err']}.")
    if per.get("srcErr"):
        st.warning(f"HaraCRM: {per['srcErr']}")
    ptabs = per.get("tables", [])
    srcs_ = [x for x in dict.fromkeys(t_.get("src") or "Tất cả" for t_ in ptabs)]
    if len(srcs_) > 1:
        src_pick = st.segmented_control("Nguồn (HaraCRM)", srcs_, default=srcs_[0], key="hara_src") or srcs_[0]
        ptabs = [t_ for t_ in ptabs if (t_.get("src") or "Tất cả") == src_pick]
    h = {"tables": ptabs}
    want = [_plain(x) for x in secret("haracrm", "tables", ["Xếp hạng funnel", "NV trong phạm vi lọc"])]
    shown = [t for t in h.get("tables", []) if not want or any(w in _plain(t.get("title", "")) for w in want)]
    shown.sort(key=lambda t: next((i for i, w in enumerate(want) if w in _plain(t.get("title", ""))), 99))
    if not shown:
        st.warning("Dữ liệu HaraCRM không có bảng nào đúng tên cần lấy "
                   f"({', '.join(secret('haracrm', 'tables', ['Xếp hạng funnel', 'NV trong phạm vi lọc']))}).")
    for t in shown:
        st.markdown(f"**{t.get('title') or 'Bảng'}** · _{t.get('tab', '')}_")
        if t.get("sub"):
            st.caption(t["sub"])
        st.html(hara_table_html(t))


def page_leads():
    st.caption(f"Số lead **tạo trong tháng này và tháng trước** hiện đang nằm ở từng stage trên Bitrix24. "
               f"Cột **Tồn** = tổng các stage trừ {', '.join(not_open_stages) or '—'}.")
    _snap = globals().get("snap")
    if _snap and _snap.get("missingSources"):
        st.warning(f"Không thấy nguồn {_snap['missingSources']} trên Bitrix24 — sửa dòng `lead_exclude_sources` "
                   "trong Secrets cho đúng tên nguồn.")
    detail = data.get("lead_detail", pd.DataFrame(columns=LEAD_DETAIL_COLS))

    if not detail.empty:
        col_f1, col_f2, col_f3 = st.columns([1, 1.4, 1])
        all_sales_in_leads = ["Tất cả"] + [s_ for s_ in lb["sale"] if s_ in set(detail["sale"])]
        selected_sale_lead = col_f1.selectbox("🔍 Lọc theo Sale", all_sales_in_leads, key="lead_sale_filter")
        src_all = sorted(detail["source"].astype(str).unique(), key=_norm_txt)
        src_default = [x for x in src_all if not is_excluded_source(x)]
        picked_src = col_f2.multiselect("🏷️ Lọc theo Source", src_all, default=src_default, key="lead_src_filter",
                                        placeholder="Tất cả source",
                                        help="Mặc định bỏ: " + (", ".join(lead_exclude_sources()) or "—")
                                             + ". Để trống = tất cả source.")
        l_from = date.fromisoformat((_snap or {}).get("ldetFrom") or f"{_prev_month_start(now.date()):%Y-%m-%d}")
        t_opts = ["Tháng này", "Tháng trước", "Cả 2 tháng", "Hôm nay", "Hôm qua", "Tuần này", "Tùy chọn khoảng ngày"]
        t_mode = col_f3.selectbox("📅 Ngày tạo lead", t_opts, key="lead_time_filter",
                                  help=f"Dữ liệu có lead tạo từ {l_from:%d/%m/%Y} (tháng trước và tháng này).")
        f = detail
        if picked_src:
            f = f[f["source"].astype(str).isin(picked_src)]
        m0 = now.date().replace(day=1)
        if t_mode == "Cả 2 tháng":
            t0_, t1_ = l_from, now.date()
        elif t_mode == "Tháng trước":
            t0_, t1_ = _prev_month_start(now.date()), m0 - timedelta(days=1)
        elif t_mode == "Tùy chọn khoảng ngày":
            rng_ = col_f3.date_input("Từ ngày – Đến ngày", value=(l_from, now.date()), min_value=l_from,
                                     max_value=now.date(), format="DD/MM/YYYY", key="lead_time_range")
            t0_, t1_ = period_range(t_mode, now.date(), rng_)
        else:
            t0_, t1_ = period_range(t_mode, now.date())
        t0_ = max(t0_, l_from)
        f = f[f["created"].map(lambda d: isinstance(d, date) and t0_ <= d <= t1_).astype(bool)]
        col_f3.caption(f"Từ {t0_:%d/%m/%Y} đến {min(t1_, now.date()):%d/%m/%Y}")
        tbl = (f.pivot_table(index="sale", columns="stage", values="n", aggfunc="sum")
               .reindex(index=[s_ for s_ in lb["sale"]], columns=stages).fillna(0).astype(int))
        stage_view_full = tbl.copy()
        stage_view_full.insert(0, "Tồn", tbl[open_stages].sum(axis=1))
        stage_view_full["Tổng"] = tbl[stages].sum(axis=1)
        stage_view_full = stage_view_full.reset_index().rename(columns={"sale": "Sale", "index": "Sale"})
        _render_lead_table(stage_view_full, selected_sale_lead)
        _render_unqual_reasons(f, selected_sale_lead, _snap)
        lost = data.get("deal_lost", pd.DataFrame(columns=DEAL_LOST_COLS))
        if picked_src:
            lost = lost[lost["source"].astype(str).isin(picked_src)]
        lost = lost[lost["closed"].map(lambda d: isinstance(d, date) and t0_ <= d <= t1_).astype(bool)]
        _render_lost_reasons(lost, selected_sale_lead, _snap)
        return

    if _snap is not None and "ldet" not in _snap:
        st.info("Lọc theo Source / thời gian cần dữ liệu mới: leader cài lại nút **📥 Cập nhật Bitrix** "
                "(trang Cập nhật Bitrix) rồi bấm nút trên Bitrix24.")
    if stage_tbl.empty:
        st.info("Chưa có dữ liệu lead tồn theo stage.")
        return

    stage_view_full = stage_tbl.loc[lb["sale"]].rename(columns={"lead_open": "Tồn"}) if "sale" in stage_tbl.index.names else stage_tbl.copy().rename(columns={"lead_open": "Tồn"})
    existing_stages = [s for s in stages if s in stage_view_full.columns]
    stage_view_full["Tổng"] = stage_view_full[existing_stages].sum(axis=1)
    
    stage_view_full = stage_view_full.reset_index()
    if "index" in stage_view_full.columns:
        stage_view_full = stage_view_full.rename(columns={"index": "Sale"})
    elif "sale" in stage_view_full.columns:
        stage_view_full = stage_view_full.rename(columns={"sale": "Sale"})
        
    stage_view_full = stage_view_full.loc[:, ~stage_view_full.columns.duplicated()]

    col_f1, _ = st.columns([1, 2])
    all_sales_in_leads = ["Tất cả"] + sorted(stage_view_full["Sale"].unique().tolist())
    selected_sale_lead = col_f1.selectbox("🔍 Lọc theo Sale", all_sales_in_leads, key="lead_sale_filter")
    _render_lead_table(stage_view_full, selected_sale_lead)


def _reason_table(u: pd.DataFrame, total_col: str) -> pd.DataFrame:
    u = u.copy()
    u["reason"] = u["reason"].replace("", "(Chưa ghi lý do)").fillna("(Chưa ghi lý do)")
    pv = u.pivot_table(index="sale", columns="reason", values="n", aggfunc="sum", fill_value=0)
    order_ = [c for c in pv.sum().sort_values(ascending=False).index if c != "(Chưa ghi lý do)"]
    pv = pv[order_ + [c for c in pv.columns if c == "(Chưa ghi lý do)"]]
    pv.insert(0, total_col, pv.sum(axis=1))
    pv = pv.sort_values(total_col, ascending=False).reset_index().rename(columns={"sale": "Sale"})
    pv.columns.name = None
    if len(pv) > 1:
        tot = pv.drop(columns="Sale").sum().to_frame().T
        tot.insert(0, "Sale", "TỔNG")
        pv = pd.concat([pv, tot], ignore_index=True)
    num = [c for c in pv.columns if c != "Sale"]
    pv[num] = pv[num].astype(int)
    return pv


def _show_reason_table(pv: pd.DataFrame, total_col: str, help_: str, fname: str, label: str, first: str = "Sale",
                       pct: str | None = None):
    pv = pv.rename(columns={"Sale": first}) if first != "Sale" else pv
    num = [c for c in pv.columns if c not in (first, pct)]
    disp = pv.assign(**{first: pv[first].astype(str).str.replace(" ", "\u00a0")}) if first == "Sale" else pv.copy()
    styled = (disp.style
              .hide(axis="index")
              .set_properties(subset=[total_col], **{"background-color": "rgba(255, 193, 7, 0.22)", "font-weight": "bold"})
              .set_properties(subset=num, **{"text-align": "right"})
              .set_table_styles([{"selector": "th", "props": [("white-space", "normal"), ("word-break", "break-word"),
                                                               ("vertical-align", "bottom"), ("min-width", "70px")]},
                                 {"selector": "td", "props": [("white-space", "normal")]},
                                 {"selector": "td.col0", "props": [("white-space", "nowrap")]}])
              .apply(lambda r: ["font-weight: bold" if r[first] == "TỔNG" else "" for _ in r], axis=1)
              .format(lambda v: f"{v:,}" if v else "", subset=num))
    if pct:
        styled = styled.format("{:.0%}", subset=[pct]).set_properties(subset=[pct], **{"text-align": "right"})
    if first != "Sale":
        styled = styled.set_table_styles([{"selector": "td.col0", "props": [("white-space", "normal"),
                                                                            ("min-width", "180px")]}], overwrite=False)
    st.table(styled)  # bảng thường: tiêu đề dài tự xuống hàng thay vì bị cắt
    st.download_button(label, pv.to_csv(index=False).encode("utf-8-sig"), file_name=fname, mime="text/csv")


def _old_button_msg(snap_: dict):
    at_ = datetime.fromtimestamp(snap_.get("at", 0) / 1000, TZ)
    st.warning(f"Dữ liệu Bitrix hiện có (gửi lúc **{at_:%H:%M %d/%m}**) đến từ **nút Cập nhật Bitrix cũ** nên chưa có phần này. "
               "Leader làm lại: mở trang **📥 Cập nhật Bitrix** → xóa nút cũ trên thanh dấu trang → kéo nút mới vào → "
               "mở Bitrix24, bấm nút mới → **📤 Gửi sang dashboard**.")


def _show_reason_list(u: pd.DataFrame, fname: str, label: str, detail_label: str = "Lý do chi tiết"):
    """Lý do nhập tay: mỗi lý do 1 hàng, nhóm theo Sale; kèm Nhu cầu ban đầu + Unqualified reason nếu có."""
    u = u.copy()
    u["reason"] = u["reason"].map(lambda v: " ".join(str(v).split())).replace("", "(Chưa ghi lý do)")
    has_need = "need" in u and u["need"].fillna("").astype(str).str.strip().ne("").any()
    has_main = "main_reason" in u and u["main_reason"].fillna("").astype(str).str.strip().ne("").any()
    keys = ["sale"]
    if has_need:
        u["need"] = u["need"].fillna("").astype(str).str.strip().replace("", "(Chưa ghi nhu cầu)")
        needs = list(u.groupby("need")["n"].sum().sort_values(ascending=False).index)
        pick = st.selectbox("🎯 Lọc theo Nhu cầu ban đầu", ["Tất cả"] + needs, key=f"rl_need_{fname}")
        if pick != "Tất cả":
            u = u[u["need"] == pick]
        keys.append("need")
    if has_main:
        u["main_reason"] = u["main_reason"].fillna("").astype(str).replace("", "(Chưa ghi reason)")
        keys.append("main_reason")
    keys.append("reason")
    g = u.groupby(keys, as_index=False)["n"].sum()
    rank = {s_: i for i, s_ in enumerate(lb["sale"])}
    g["_s"] = g["sale"].map(rank).fillna(len(rank))
    g["_blank"] = g["reason"].eq("(Chưa ghi lý do)")
    g = g.sort_values(["_s"] + keys[1:-1] + ["_blank", "n", "reason"],
                      ascending=[True] + [True] * len(keys[1:-1]) + [True, False, True])
    names = {"sale": "Sale", "need": "Nhu cầu ban đầu", "main_reason": "Unqualified reason",
             "reason": detail_label, "n": "Số lead"}
    out = g.rename(columns=names)[[names[k] for k in keys] + ["Số lead"]]
    tot = out.groupby("Sale", sort=False)["Số lead"].sum()
    st.caption("Tổng lead Unqualified: " + " · ".join(f"**{k}** {v}" for k, v in tot.items()))
    out = out.reset_index(drop=True)
    disp = out.copy()
    disp["Sale"] = disp["Sale"].astype(str).str.replace(" ", "\u00a0")
    disp.loc[disp["Sale"].eq(disp["Sale"].shift()), "Sale"] = ""   # chỉ ghi tên Sale ở dòng đầu của nhóm
    styled = (disp.style.hide(axis="index")
              .set_properties(subset=["Số lead"], **{"text-align": "right", "font-weight": "bold"})
              .set_table_styles([{"selector": "td", "props": [("white-space", "normal"), ("vertical-align", "top")]}]))
    with st.container(height=min(600, 80 + 36 * len(disp)), border=False):
        st.table(styled)
    st.download_button(label, out.to_csv(index=False).encode("utf-8-sig"), file_name=fname, mime="text/csv")


def _render_lost_reasons(lost: pd.DataFrame, selected_sale: str, snap_: dict | None):
    st.subheader("💔 Deal | Lý do Lost theo Sale")
    if snap_ is not None and "dlost" not in snap_:
        _old_button_msg(snap_)
        return
    if snap_ is not None and not snap_.get("lostField"):
        cands = snap_.get("lostCands") or []
        st.warning("Chưa tìm thấy trường **lý do Lost** của deal trên Bitrix24 (bảng dưới chỉ có số deal Lost). "
                   'Điền vào Secrets, mục `[bitrix24]`: `lost_reason_field = "Tên trường"`, rồi cài lại nút Cập nhật Bitrix.'
                   + (f" Các trường tự tạo của deal: {', '.join(cands)}." if cands else ""))
    elif snap_:
        st.caption(f"Deal thất bại (Lost), lấy lý do từ trường **{snap_['lostField'][1]}**. "
                   "Áp dụng bộ lọc Sale / Source; bộ lọc thời gian tính theo ngày đóng deal.")
    if selected_sale != "Tất cả":
        lost = lost[lost["sale"] == selected_sale]
    if lost.empty:
        st.caption("Không có deal Lost nào với bộ lọc hiện tại.")
        return
    _show_reason_table(_reason_table(lost, "Lost"), "Lost", "Tổng deal Lost",
                       f"ly_do_lost_{now:%Y%m%d}.csv", "⬇️ Tải bảng lý do Lost (CSV)")


def _render_unqual_reasons(f: pd.DataFrame, selected_sale: str, snap_: dict | None):
    if snap_ is not None and "reasonField" not in snap_:
        st.subheader("🚫 Lead | Lý do Unqualified theo Sale")
        _old_button_msg(snap_)
        return
    fields = (snap_.get("reasonFields") or ([snap_["reasonField"]] if snap_.get("reasonField") else [])
              if snap_ is not None else [["", "Unqualified reason"], ["", "Chi tiết lý do Unqualified khách hàng"]])
    if snap_ is not None and snap_.get("reasonMissing"):
        st.warning(f"Không thấy trường {snap_['reasonMissing']} trên Bitrix24 — sửa `unqual_reason_field` trong Secrets.")
    if not fields:
        st.subheader("🚫 Lead | Lý do Unqualified theo Sale")
        cands = snap_.get("reasonCands") or []
        st.warning("Chưa tìm thấy trường **lý do Unqualified** trên Bitrix24. Điền vào Secrets, mục `[bitrix24]`: "
                   '`unqual_reason_field = ["Unqualified reason", "Chi tiết unqualified"]`, rồi cài lại nút Cập nhật Bitrix.'
                   + (f" Các trường tự tạo hiện có: {', '.join(cands)}." if cands else ""))
        return
    closed_l_ = [c.lower() for c in closed_stages]
    u0 = f[f["stage"].astype(str).str.lower().isin(closed_l_)]
    if selected_sale != "Tất cả":
        u0 = u0[u0["sale"] == selected_sale]
    order = sorted(enumerate(fields), key=lambda t: (0 if "reason" in _plain(t[1][1]) else 1, t[0]))  # "Unqualified reason" lên trước
    ridx = order[0][0]
    need_done = False
    for idx, (code, label) in order:
        is_detail = "chi tiet" in _plain(label) or "DETAIL" in str(code).upper()
        if is_detail and not need_done:
            _render_unqual_by_need(f, selected_sale, snap_)
            need_done = True
        st.subheader(f"🚫 Lead | {label} theo Sale")
        st.caption(f"Lead đang ở stage {', '.join(closed_stages)}, lấy từ trường **{label}**"
                   f"{f' (`{code}`)' if code else ''}. Áp dụng cùng bộ lọc Sale / Source / Ngày tạo ở trên.")
        if u0.empty:
            st.caption("Không có lead Unqualified nào với bộ lọc hiện tại.")
            continue
        u = u0.copy()
        u["reason"] = u["reason"].map(lambda v: (v[idx] if isinstance(v, (list, tuple)) and len(v) > idx
                                                 else (v if idx == 0 and isinstance(v, str) else "")))
        safe = re.sub(r"\W+", "_", _norm_txt(label)) or f"truong_{idx + 1}"
        if is_detail:   # lý do sale tự nhập tay → bảng kết hợp Nhu cầu + Unqualified reason + chi tiết
            u["main_reason"] = u0["reason"].map(lambda v: (v[ridx] if isinstance(v, (list, tuple)) and len(v) > ridx
                                                           else (v if isinstance(v, str) else "")))
            _show_reason_list(u, f"unqualified_{safe}_{now:%Y%m%d}.csv", f"⬇️ Tải bảng {label} (CSV)",
                              detail_label=label)
        else:
            _show_reason_table(_reason_table(u, "Unqual"), "Unqual", "Tổng lead Unqualified",
                               f"unqualified_{safe}_{now:%Y%m%d}.csv", f"⬇️ Tải bảng {label} (CSV)")
    if not need_done:
        _render_unqual_by_need(f, selected_sale, snap_)


def _render_unqual_by_need(f: pd.DataFrame, selected_sale: str, snap_: dict | None):
    """Lead Unqualified chia theo nhu cầu ban đầu (trường trên lead Bitrix), kèm lý do Unqualified."""
    st.subheader("🎯 Lead | Unqualified theo Nhu cầu ban đầu")
    if snap_ is not None and "needFields" not in snap_:
        st.info("Cần cài lại nút **📥 Cập nhật Bitrix** (phiên bản mới) rồi bấm trên Bitrix24 để đọc trường nhu cầu ban đầu.")
        return
    nf = (snap_ or {}).get("needFields") or ([["", "Nhu cầu ban đầu"]] if snap_ is None else [])
    if snap_ is not None and snap_.get("needMissing"):
        st.warning(f"Không thấy trường {', '.join(snap_['needMissing'])} trên lead Bitrix24 — sửa `need_field` trong "
                   "Secrets (mục `[bitrix24]`) rồi cài lại nút.")
    if not nf:
        return
    st.caption(f"Nhu cầu ban đầu lấy từ trường **{' / '.join(x[1] for x in nf)}** trên lead (phần sau chữ "
               "«Đang quan tâm …», đến dấu chấm). Lead ở stage "
               f"{', '.join(closed_stages)}; cùng bộ lọc Sale / Source / Ngày tạo ở trên.")
    u = f[f["stage"].astype(str).str.lower().isin([c.lower() for c in closed_stages])].copy()
    if selected_sale != "Tất cả":
        u = u[u["sale"] == selected_sale]
    if u.empty:
        st.caption("Không có lead Unqualified nào với bộ lọc hiện tại.")
        return
    u["need"] = u["need"].fillna("").astype(str).str.strip().replace("", "(Chưa ghi nhu cầu)")
    pv = u.pivot_table(index="need", columns="sale", values="n", aggfunc="sum", fill_value=0)
    pv = pv[[s_ for s_ in lb["sale"] if s_ in pv.columns]]
    pv.insert(0, "Unqual", pv.sum(axis=1))
    pv = pv.sort_values("Unqual", ascending=False)
    pv.insert(1, "% Unqual", pv["Unqual"] / pv["Unqual"].sum())
    pv = pv.reset_index().rename(columns={"need": "Sale"})
    pv.columns.name = None
    tot = pv.drop(columns=["Sale"]).sum().to_frame().T
    tot.insert(0, "Sale", "TỔNG")
    pv = pd.concat([pv, tot], ignore_index=True)
    ints = [c for c in pv.columns if c not in ("Sale", "% Unqual")]
    pv[ints] = pv[ints].astype(int)
    st.caption("Hàng = nhu cầu ban đầu, cột = từng Sale.")
    _show_reason_table(pv, "Unqual", "Tổng lead Unqualified", f"unqualified_theo_nhu_cau_{now:%Y%m%d}.csv",
                       "⬇️ Tải bảng Unqualified theo nhu cầu (CSV)", first="Nhu cầu ban đầu", pct="% Unqual")


def _render_lead_table(stage_view_full: pd.DataFrame, selected_sale_lead: str):
    filtered_stage_df = stage_view_full.copy()
    if selected_sale_lead != "Tất cả":
        filtered_stage_df = filtered_stage_df[filtered_stage_df["Sale"] == selected_sale_lead]
        
    filtered_stage_df = filtered_stage_df.sort_values("Tồn", ascending=False)
    
    if selected_sale_lead == "Tất cả" and len(filtered_stage_df) > 1:
        num_cols = filtered_stage_df.select_dtypes(include=[np.number]).columns
        sum_row = filtered_stage_df[num_cols].sum().to_frame().T
        sum_row["Sale"] = "TỔNG"
        filtered_stage_df = pd.concat([filtered_stage_df, sum_row], ignore_index=True)
        
    st.markdown(f"Hiển thị **{len(filtered_stage_df[filtered_stage_df['Sale'] != 'TỔNG'])}** nhân sự")
    
    num_cols_ = [c for c in filtered_stage_df.columns if c != "Sale"]
    filtered_stage_df[num_cols_] = filtered_stage_df[num_cols_].fillna(0).astype(int)
    styled = (filtered_stage_df.style
              .set_properties(subset=["Tổng"], **{"background-color": "rgba(255, 193, 7, 0.22)",
                                                  "font-weight": "bold"})
              .format("{:,.0f}", subset=num_cols_))
    st.dataframe(
        styled, 
        hide_index=True, 
        width="stretch", 
        column_config={
            "Sale": st.column_config.TextColumn("Sale", pinned=True),
            "Tồn": st.column_config.NumberColumn(help="Tổng lead chưa ra kết quả"),
            "Tổng": st.column_config.NumberColumn(help="Tổng tất cả stage trong bảng"),
            **{c: st.column_config.NumberColumn(help="Không tính vào Tồn") for c in not_open_stages if c in stages},
        }
    )
    
    st.download_button(
        "⬇️ Tải bảng lead (CSV)", 
        filtered_stage_df.to_csv(index=False).encode("utf-8-sig"),
        file_name=f"lead_ton_theo_stage_{now:%Y%m%d}.csv", 
        mime="text/csv"
    )


def _meetings_for(d0: date, d1: date) -> pd.DataFrame:
    """Meeting + trạng thái recap cho khoảng ngày riêng của trang Meeting (khác kỳ ở thanh bên)."""
    if DEMO:
        m = demo_data(d0, d1)["meetings"]
    else:
        if not ((g_leader or g_sa) and not roster.empty):
            return mt.iloc[0:0]
        m = (load_meetings_leader if g_leader else load_meetings)(
            tuple(roster["email"]), tuple(roster["sale"]), d0, d1)
        m = m.assign(recap=pd.NA, est_amount=0.0, recap_delay_str="—", recap_msg_id="")
        if gmail_on and not m.empty:
            em = load_recap_emails(tuple(sorted(set(roster["email"]))), d0, d1, g_leader)
            if em is not None:
                rc = match_recaps(m, em)
                m = m.drop(columns=["recap", "est_amount", "recap_delay_str", "recap_msg_id", "recap_link"],
                           errors="ignore").merge(rc, on="event_id", how="left")
    m = m[m["sale"].isin(sales_in_view)].copy()
    m["status"] = m.apply(recap_status, axis=1) if not m.empty else pd.Series(dtype=str)
    return m


def page_deal_open():
    d = data.get("deal_open", pd.DataFrame(columns=DEAL_OPEN_COLS))
    _snap = globals().get("snap")
    if _snap is not None and "dopen" not in _snap:
        st.warning("Cần dữ liệu mới: leader cài lại nút **📥 Cập nhật Bitrix** (trang Cập nhật Bitrix) rồi bấm trên Bitrix24.")
        return
    st.caption("Deal **đang xử lý** (chưa Won / Lost) hiện nằm ở từng stage trên Bitrix24, tại thời điểm bấm nút cập nhật.")
    c1, c2 = st.columns([1, 1])
    s_pick = c1.selectbox("🔍 Lọc theo Sale", ["Tất cả"] + list(lb["sale"]), key="do_sale")
    t_opts = ["Tất cả thời gian", "Hôm nay", "Hôm qua", "Tuần này", "Tháng này", "Tháng trước", "Quý này",
              "Tùy chọn khoảng ngày"]
    t_mode = c2.selectbox("📅 Ngày tạo deal", t_opts, key="do_time")
    if s_pick != "Tất cả":
        d = d[d["sale"] == s_pick]
    if t_mode != "Tất cả thời gian":
        rng_ = None
        if t_mode == "Tùy chọn khoảng ngày":
            rng_ = c2.date_input("Từ ngày – Đến ngày", value=(now.date() - timedelta(days=30), now.date()),
                                 format="DD/MM/YYYY", key="do_range")
        t0_, t1_ = period_range(t_mode, now.date(), rng_)
        d = d[d["created"].map(lambda x: isinstance(x, date) and t0_ <= x <= t1_).astype(bool)]
        c2.caption(f"Deal tạo từ {t0_:%d/%m/%Y} đến {t1_:%d/%m/%Y}")
    if d.empty:
        st.info("Không có deal tồn nào với bộ lọc hiện tại.")
        return
    order = (d[["stage", "order"]].sort_values("order").drop_duplicates("stage")["stage"].tolist())  # mọi pipeline
    k = st.columns(min(len(order), 6) or 1)
    for i_, stg in enumerate(order[:6]):
        k[i_].metric(stg, fmt_int(d.loc[d["stage"] == stg, "n"].sum()))
    rows = [s_ for s_ in lb["sale"] if s_ in set(d["sale"])]
    tbl = (d.pivot_table(index="sale", columns="stage", values="n", aggfunc="sum", fill_value=0)
           .reindex(index=rows, columns=order, fill_value=0))
    tbl["Tổng"] = tbl.sum(axis=1)
    if len(tbl) > 1:
        tbl.loc["TỔNG"] = tbl.sum()
    tbl = tbl.reset_index().rename(columns={"sale": "Sale", "index": "Sale"})
    tbl.columns.name = None
    num = [c for c in tbl.columns if c != "Sale"]
    tbl[num] = tbl[num].astype(int)
    sty = (tbl.style
           .set_properties(subset=["Tổng"], **{"background-color": "rgba(255, 193, 7, 0.22)", "font-weight": "bold"})
           .apply(lambda r: ["font-weight: bold; background-color: rgba(62, 180, 137, 0.18)"] * len(r)
                  if r["Sale"] == "TỔNG" else [""] * len(r), axis=1)
           # số bấm được: chữ xanh như link; ô 0 làm mờ
           .map(lambda v: "color: rgb(28, 131, 225); font-weight: 700;" if v else "color: rgba(128, 128, 128, 0.55);",
                subset=num))
    st.info("Bấm vào **ô số màu xanh** để xem danh sách deal ngay bên dưới bảng.", icon=":material/touch_app:")
    ev = st.dataframe(sty, hide_index=True, width="stretch", on_select="rerun", selection_mode="single-cell",
                      key="do_table", column_config={"Sale": st.column_config.TextColumn(pinned=True)})
    st.download_button("⬇️ Tải bảng deal tồn (CSV)", tbl.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"deal_ton_{now:%Y%m%d}.csv", mime="text/csv")

    cells = list(getattr(getattr(ev, "selection", None), "cells", []) or [])
    if not cells:
        return
    r_i, col_ = cells[0][0], cells[0][1]
    sale_ = tbl.iloc[int(r_i)]["Sale"]
    if col_ == "Sale":
        col_ = "Tổng"
    lst = d.copy()
    if sale_ != "TỔNG":
        lst = lst[lst["sale"] == sale_]
    if col_ != "Tổng":
        lst = lst[lst["stage"] == col_]
    who = "cả team" if sale_ == "TỔNG" else sale_
    where = "tất cả stage" if col_ == "Tổng" else f"stage **{col_}**"
    st.subheader(f"📋 {int(lst['n'].sum())} deal tồn — {who} · {where}", anchor="ds-deal-ton")
    import streamlit.components.v1 as components
    components.html(  # tự cuộn xuống danh sách mỗi lần bấm ô khác (nonce đổi theo ô được chọn)
        f"""<script>/*{r_i}-{col_}*/setTimeout(()=>{{const e=window.parent.document.getElementById('ds-deal-ton');
        if(e)e.scrollIntoView({{behavior:'smooth',block:'start'}});}},250);</script>""", height=0)
    if lst["id"].astype(str).eq("").all():
        st.info("Danh sách từng deal cần dữ liệu mới: leader cài lại nút **📥 Cập nhật Bitrix** rồi bấm trên Bitrix24.")
        return
    age = (pd.Timestamp(now) - lst["created_ts"])
    view = pd.DataFrame({
        "Ngày tạo deal": lst["created_ts"].dt.tz_localize(None),
        "Sale": lst["sale"], "Tên deal": lst["title"], "Stage": lst["stage"],
        "Gói dịch vụ": lst["package"].fillna("").replace("", "—"),
        "Số tiền (₫)": lst["amount"],
        "Tồn (ngày)": (age.dt.total_seconds() / 86400).round(1),
        "Cập nhật lần cuối": lst["modified_ts"].dt.tz_localize(None),
        "Chưa chạm (ngày)": ((pd.Timestamp(now) - lst["modified_ts"]).dt.total_seconds() / 86400).round(1),
        "Link deal": lst["link"],
        "_cm": lst["comments"] if "comments" in lst else [[]] * len(lst),
    }).sort_values("Ngày tạo deal")
    show_stage, show_sale = lst["stage"].nunique() > 1, lst["sale"].nunique() > 1   # 1 giá trị → đã ghi ở tiêu đề
    st.caption(f"Tổng giá trị: **{fmt_vnd(lst['amount'].sum())}** · rê chuột vào ô **Comment gần nhất** để xem "
               "lịch sử comment trên deal")
    num = lambda v, f="{:.1f}": "" if pd.isna(v) else f.format(v)
    hdr = (["Ngày tạo"] + (["Sale"] if show_sale else []) + ["Tên deal"] + (["Stage"] if show_stage else [])
           + ["Gói dịch vụ", "Số tiền (₫)", "Tồn (ngày)", "Cập nhật cuối", "Chưa chạm (ngày)", "Comment gần nhất"])
    cls = (["nw"] + (["nw"] if show_sale else []) + ["w3"] + (["w4"] if show_stage else [])
           + ["", "n", "n", "nw", "n", "c"])
    rows = []
    for r in view.itertuples(index=False):
        name = (f'<a href="{escape(r[9])}" target="_blank">{escape(str(r[2]))} ↗</a>' if r[9] else escape(str(r[2])))
        rows.append([f"{r[0]:%d/%m/%y}" if not pd.isna(r[0]) else ""] + ([escape(str(r[1]))] if show_sale else [])
                    + [name] + ([escape(str(r[3]))] if show_stage else [])
                    + [escape(str(r[4])), f"{r[5]:,.0f}".replace(",", "."), num(r[6]),
                       f"{r[7]:%d/%m/%y %H:%M}" if not pd.isna(r[7]) else "", num(r[8]), comment_cell(r[10])])
    st.html(pop_table(hdr, rows, cls))
    view = view.assign(**{"Comment gần nhất": view["_cm"].map(fmt_comments)}).drop(columns=["_cm"])
    st.download_button("⬇️ Tải danh sách deal (CSV)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"deal_ton_chi_tiet_{now:%Y%m%d}.csv", mime="text/csv", key="do_list_dl")


def page_meetings():
    c1, c2 = st.columns([1, 1])
    sale_opts = ["Tất cả"] + list(lb["sale"])
    m_sale = c1.selectbox("🔍 Lọc theo Sale", sale_opts, key="mt_sale_filter")
    t_opts = ["Theo kỳ ở thanh bên", "Hôm nay", "Hôm qua", "Tuần này", "Tháng này", "Tháng trước", "Quý này",
              "Tùy chọn khoảng ngày"]
    m_time = c2.selectbox("📅 Thời gian meeting", t_opts, key="mt_time_filter")
    if m_time == "Theo kỳ ở thanh bên":
        m0, m1, mm = d_start, d_end, mt
    else:
        rng_ = None
        if m_time == "Tùy chọn khoảng ngày":
            rng_ = c2.date_input("Từ ngày – Đến ngày", value=(now.date() - timedelta(days=7), now.date()),
                                 format="DD/MM/YYYY", key="mt_time_range")
        m0, m1 = period_range(m_time, now.date(), rng_)
        try:
            mm = _meetings_for(m0, m1)
        except Exception as e:
            st.warning(f"Không tải được meeting cho khoảng ngày này: {type(e).__name__}: {e}")
            mm = mt.iloc[0:0]
    c2.caption(f"Từ {m0:%d/%m/%Y} đến {m1:%d/%m/%Y}")
    if m_sale != "Tất cả":
        mm = mm[mm["sale"] == m_sale]

    cnt = mm["status"].value_counts() if not mm.empty else pd.Series(dtype=int)
    done, miss = int(cnt.get(ST_DONE, 0)), int(cnt.get(ST_MISS, 0))
    k = st.columns(5)
    k[0].metric("📅 Meeting", fmt_int(len(mm)))
    k[1].metric("✅ Đã recap", fmt_int(done))
    k[2].metric("❌ Thiếu recap", fmt_int(miss))
    k[3].metric("✉️ Tỷ lệ recap", f"{done / (done + miss):.0%}" if done + miss else "—")
    k[4].metric("💰 Tổng Est", fmt_vnd(mm["est_amount"].sum() if "est_amount" in mm else 0))

    order = [ST_DONE, ST_MISS, ST_UPCOMING, ST_UNKNOWN]
    present = [s for s in order if cnt.get(s, 0) > 0]
    if mm.empty:
        st.caption("Không có meeting nào với bộ lọc hiện tại.")
        return
    counts = mm.groupby(["sale", "status"]).size().unstack(fill_value=0)
    sales_order = [s_ for s_ in lb["sale"] if s_ in counts.index]
    stack = (counts.reindex(index=sales_order, columns=present, fill_value=0)
             .reset_index().melt(id_vars="sale", var_name="Trạng thái", value_name="Số meeting"))

    chart_height = max(220, min(350, 45 * len(sales_order) + 80))
    fig = px.bar(
        stack, x="Số meeting", y="sale", color="Trạng thái", orientation="h",
        color_discrete_map=STATUS_COLORS, category_orders={"Trạng thái": present},
        labels={"sale": "", "Số meeting": "Số lượng", "Trạng thái": ""}
    )
    fig.update_traces(marker=dict(line=dict(width=0)), hovertemplate="<b>%{y}</b><br>%{fullData.name}: <b>%{x}</b> meeting<extra></extra>")
    fig.update_layout(
        height=chart_height, margin=dict(l=10, r=20, t=20, b=10),
        barmode="stack", bargap=0.4, paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, title=None, font=dict(size=12)),
        xaxis=dict(showgrid=True, gridcolor="rgba(200, 200, 200, 0.2)", zeroline=False),
        yaxis=dict(showgrid=False, categoryorder="total ascending")
    )
    st.plotly_chart(fig, width="stretch")

    # ── Meeting → Deal: diễn biến sau meeting
    deals_all = data.get("deal_all", pd.DataFrame(columns=DEAL_ALL_COLS))
    md = match_meeting_deals(mm, deals_all)
    mm = mm.merge(md, on="event_id", how="left") if not md.empty else mm.assign(outcome="⚪ Chưa nối deal")
    past = mm[mm["ts"] <= pd.Timestamp(now)] if "ts" in mm else mm
    st.subheader("🎯 Meeting → Deal: diễn biến sau meeting", anchor="meeting-deal")
    st.caption("Mỗi meeting được nối với deal Bitrix của cùng Sale: ưu tiên **link deal dán trong mô tả sự kiện lịch**, "
               "nếu không có thì tự khớp tên khách trong tiêu đề meeting với tên deal. "
               "🟢 Đã chốt · 🔵 Đang đàm phán · 🟡 Đang theo (deal được cập nhật ≤14 ngày) · "
               "🔴 Nguội (>14 ngày không chạm) · ⚫ Lost · ⚪ Chưa nối được deal.")
    if not past.empty:
        g = past.groupby("sale")
        summ = pd.DataFrame({"Meeting đã diễn ra": g.size()})
        for lab in OUTCOME_ORDER[:5]:
            summ[lab] = g["outcome"].apply(lambda x, l=lab: int((x == l).sum()))
        summ["⚪ Chưa nối deal"] = g["outcome"].apply(lambda x: int(x.astype(str).str.startswith("⚪").sum()))
        won_ = past[past["outcome"] == "🟢 Đã chốt"]
        summ["Tỷ lệ Meeting→Won"] = summ["🟢 Đã chốt"] / summ["Meeting đã diễn ra"].where(summ["Meeting đã diễn ra"] > 0)
        if not won_.empty and "deal_id" in won_:
            cl = deals_all.set_index("id")["closed"] if not deals_all.empty else pd.Series(dtype=object)
            dd = won_.assign(_c=pd.to_datetime(won_["deal_id"].map(cl), errors="coerce"))
            dd["_d"] = (dd["_c"] - dd["ts"].dt.tz_localize(None).dt.normalize()).dt.days
            summ["TB ngày Meeting→Won"] = dd[dd["_d"] >= 0].groupby("sale")["_d"].mean().round(1)
        else:
            summ["TB ngày Meeting→Won"] = np.nan
        summ = summ.reindex([s_ for s_ in lb["sale"] if s_ in summ.index])
        tot = summ.sum(numeric_only=True)
        tot["Tỷ lệ Meeting→Won"] = tot["🟢 Đã chốt"] / tot["Meeting đã diễn ra"] if tot["Meeting đã diễn ra"] else np.nan
        tot["TB ngày Meeting→Won"] = summ["TB ngày Meeting→Won"].mean()
        summ.loc["TỔNG"] = tot
        summ = summ.reset_index().rename(columns={"sale": "Sale", "index": "Sale"})
        intc = [c for c in summ.columns if c not in ("Sale", "Tỷ lệ Meeting→Won", "TB ngày Meeting→Won")]
        summ[intc] = summ[intc].fillna(0).astype(int)
        sty = (summ.style
               .format({"Tỷ lệ Meeting→Won": lambda v: "—" if pd.isna(v) else f"{v:.0%}",
                        "TB ngày Meeting→Won": lambda v: "—" if pd.isna(v) else f"{v:.1f}"})
               .apply(lambda r: ["font-weight:700;background-color:#f1f5f9" if r["Sale"] == "TỔNG" else ""] * len(r),
                      axis=1)
               .set_properties(subset=["Tỷ lệ Meeting→Won"], **{"font-weight": "700", "background-color": "#e6f7f1"}))
        st.dataframe(sty, hide_index=True, width="stretch",
                     column_config={"Sale": st.column_config.TextColumn(pinned=True)})
    st.caption("💡 Để nối chính xác 100%: Sale dán link deal Bitrix (vd. `https://<portal>/crm/deal/details/12345/`) "
               "vào **mô tả sự kiện** trên Google Calendar.")

    st.markdown("**📋 Chi tiết từng meeting**")
    ALL = "Tất cả"
    one = st.pills("Lọc trạng thái", [ALL] + order, selection_mode="single", default=ALL,
                   label_visibility="collapsed", key="mt_status_pill")
    pick = order if (not one or one == ALL) else [one]
    detail = mm[mm["status"].isin(pick)]
    oc_opts = [ALL] + [o for o in OUTCOME_ORDER if (detail["outcome"] == o).any()]
    oc = st.pills("Lọc kết quả deal", oc_opts, selection_mode="single", default=ALL,
                  label_visibility="collapsed", key="mt_outcome_pill")
    if oc and oc != ALL:
        detail = detail[detail["outcome"] == oc]
    detail = detail.sort_values("ts", ascending=False)
    col_ = lambda c, d="": detail[c] if c in detail else pd.Series(d, index=detail.index)
    st.caption(f"{len(detail)} meeting · rê chuột vào ô **Comment gần nhất** để xem lịch sử comment trên deal")

    def _money(v):
        v = pd.to_numeric(v, errors="coerce")
        return "" if pd.isna(v) or not v else fmt_vnd(v)

    rows = []
    for r in detail.itertuples():
        g = lambda c, d="": getattr(r, c, d) if not (isinstance(getattr(r, c, d), float) and pd.isna(getattr(r, c, d))) else d
        rlink, subj = g("recap_link"), g("recap_subject")
        recap = escape(str(r.status))
        if g("recap_delay_str") not in ("", "—"):
            recap += f'<div class="mut">sau {escape(str(g("recap_delay_str")))}</div>'
        if rlink:
            recap += f'<div><a href="{escape(rlink)}" target="_blank" title="{escape(str(subj))}">🔎 Mở recap</a></div>'
        dt, dl = str(g("deal_title")), g("deal_link")
        deal = (f'<a href="{escape(dl)}" target="_blank">{escape(dt)} ↗</a>' if dl and dt else escape(dt))
        if g("deal_by"):
            deal += f'<div class="mut">nối theo {escape(str(g("deal_by")))}</div>'
        unt = pd.to_numeric(g("deal_untouched", None), errors="coerce")
        rows.append([
            f"{pd.Timestamp(r.ts):%d/%m %H:%M}", escape(str(r.sale)), escape(str(r.title)), recap,
            _money(g("est_amount", 0)), escape(str(g("outcome") or "⚪ Chưa nối deal")), deal,
            escape(str(g("deal_stage"))), "" if pd.isna(unt) else f"{unt:.0f}", _money(g("deal_amount", None)),
            comment_cell(g("deal_comments", []), short=40),
        ])
    st.html(pop_table(
        ["Thời gian", "Sale", "Meeting", "Recap", "Est", "Kết quả", "Deal", "Stage hiện tại", "Chưa chạm (ngày)",
         "Giá trị deal", "Comment gần nhất"],
        rows, ["nw", "", "w1", "w2", "n", "w5", "w3", "w4", "n", "n", "c"]))
    csv = pd.DataFrame({
        "Thời gian": detail["ts"].dt.tz_localize(None), "Sale": detail["sale"], "Meeting": detail["title"],
        "Trạng thái recap": detail["status"],
        "Email recap": detail["recap_subject"] if "recap_subject" in detail else "",
        "Est Doanh thu": detail["est_amount"] if "est_amount" in detail else 0,
        "Kết quả": col_("outcome", ""), "Deal": col_("deal_title"), "Stage": col_("deal_stage"),
        "Giá trị deal": col_("deal_amount", None), "Chưa chạm (ngày)": col_("deal_untouched", None),
        "Link deal": col_("deal_link"), "Comment gần nhất": col_("deal_comments", None).map(fmt_comments),
    })
    st.download_button("⬇️ Tải danh sách meeting (CSV)", csv.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"meeting_deal_{now:%Y%m%d}.csv", mime="text/csv", key="mt_list_dl")


def page_all_recap_emails():
    st.subheader("📬 Danh sách tất cả email recap đọc từ Gmail")
    if raw_emails_df is None or raw_emails_df.empty:
        st.info("Chưa có email recap nào được tìm thấy.")
        return
        
    email2sale = dict(zip(roster["email"], roster["sale"])) if not roster.empty else {}
    rows = []
    for em in raw_emails_df.itertuples():
        s_email = str(em.sender).lower()
        sale_name = email2sale.get(s_email, s_email)
        mid = str(getattr(em, "message_id", "") or "")
        rows.append({
            "Ngày gửi recap": em.ts.tz_localize(None) if not pd.isna(em.ts) else None,
            "Sale": sale_name,
            "Tiêu đề mail recap": em.subject,
            "Estimate doanh thu": em.est_amount,
            "Mở recap": gmail_link(getattr(em, "rfc_id", ""), mid),
        })
        
    df_display = pd.DataFrame(rows).sort_values("Ngày gửi recap", ascending=False)
    col_f1, col_f2 = st.columns([1, 2])
    all_sales_in_data = ["Tất cả"] + sorted(df_display["Sale"].unique().tolist())
    selected_sale_filter = col_f1.selectbox("🔍 Lọc theo Sale", all_sales_in_data)
    keyword_search = col_f2.text_input("🔎 Tìm kiếm từ khóa tiêu đề mail", placeholder="Nhập từ khóa...")
    
    filtered_df = df_display.copy()
    if selected_sale_filter != "Tất cả":
        filtered_df = filtered_df[filtered_df["Sale"] == selected_sale_filter]
    if keyword_search.strip():
        filtered_df = filtered_df[filtered_df["Tiêu đề mail recap"].str.lower().str.contains(keyword_search.strip().lower(), na=False)]
    
    st.dataframe(filtered_df, hide_index=True, width="stretch", column_config={
        "Ngày gửi recap": st.column_config.DatetimeColumn(format="DD/MM/YYYY HH:mm"),
        "Estimate doanh thu": st.column_config.NumberColumn(format="localized"),
        "Tiêu đề mail recap": st.column_config.TextColumn(width=420),
        "Mở recap": st.column_config.LinkColumn(
            "Mở recap", display_text="🔎 Mở trong hộp thư",
            help="Tìm đúng email này trong hộp thư Gmail của người bấm (sale: mục Đã gửi; leader: mail được CC) "
                 "— bấm vào kết quả để mở."),
    })


from html import escape

THU = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]


def day_label(ts, hourly: bool = False) -> str:
    """'T4, 11/11/2026' (hoặc 'T4, 11/11 09:00' khi xem theo giờ)."""
    ts = pd.Timestamp(ts)
    return f"{THU[ts.weekday()]}, " + (f"{ts:%d/%m %H:%M}" if hourly else f"{ts:%d/%m/%Y}")


def _link_html(text: str, url: str) -> str:
    t = escape(str(text))
    return f'<a href="{escape(url)}" target="_blank">{t}</a>' if url else t


def _hover_day_table(items: pd.DataFrame, sales_: list, fmt_item, max_items: int = 15, warn=None):
    """Bảng ngày × Sale; rê chuột vào ô số hiện danh sách mục (deal / meeting) của ô đó."""
    t0, t1 = to_dt_range(d_start, d_end)
    hourly = d_start == d_end
    freq = "h" if hourly else "D"
    idx = pd.date_range(t0, t1, freq=freq, inclusive="left")
    if hourly:
        idx = idx[(idx.hour >= 7) & (idx.hour <= 20)]
    idx = list(idx[idx <= pd.Timestamp(now).floor(freq)])[::-1]
    it = items.copy()
    if not it.empty:
        it["ts"] = pd.to_datetime(it["ts"], utc=True).dt.tz_convert(TZ)
        it["k"] = it["ts"].dt.floor(freq)
        it = it.sort_values("ts")
    cols = sales_ + ["Tổng"]
    groups = {} if it.empty else {k: g for k, g in it.groupby(["k", "sale"])}
    bykey = {} if it.empty else {k: g for k, g in it.groupby("k")}

    def cell(sub, ci, with_sale=False, bold=False):
        n = 0 if sub is None else len(sub)
        if not n:
            return '<td class="z">0</td>'
        lis = "".join(f"<li><span class=t>{pd.Timestamp(r.ts).tz_convert(TZ):%H:%M}</span> "
                      + (f"<b>{escape(str(r.sale))}</b> · " if with_sale else "") + fmt_item(r) + "</li>"
                      for r in sub.head(max_items).itertuples())
        more = f"<li class=m>… và {n - max_items} mục nữa</li>" if n > max_items else ""
        side = "r" if ci >= len(cols) / 2 else "l"
        w_ = " w" if warn is not None and warn(sub) else ""
        return (f'<td class="h{" b" if bold else ""}{w_}"><span class=n>{n}</span>'
                f'<div class="pop {side}"><ul>{lis}{more}</ul></div></td>')

    head = "<th>Ngày</th>" + "".join(f"<th>{escape(c)}</th>" for c in cols)
    tot = '<tr class="tot"><td>TỔNG</td>' + "".join(
        f'<td class="nb">{len(it) if c == "Tổng" else int((it["sale"] == c).sum()) if not it.empty else 0}</td>'
        for c in cols) + "</tr>"
    body = []
    for k in idx:
        lab = day_label(k, hourly)
        tds = "".join(cell(groups.get((k, s_)), ci) for ci, s_ in enumerate(sales_))
        tds += cell(bykey.get(k), len(cols) - 1, with_sale=True, bold=True)
        wd = pd.Timestamp(k).weekday()
        cls = "" if hourly else (" we" if wd >= 5 else "") + (" wk" if wd == 6 else "")   # T7/CN tô nền, CN kẻ ngắt tuần
        body.append(f'<tr class="{cls.strip()}"><td class=d>{lab}</td>{tds}</tr>')
    st.html(f"""<style>
.ht{{border-collapse:collapse;width:100%;font-size:14px;font-family:inherit}}
.ht th,.ht td{{border:1px solid rgba(128,128,128,.25);padding:6px 10px;text-align:right}}
.ht th{{background:rgba(62,180,137,.18);font-weight:700;text-align:center}}
.ht td.d,.ht tr.tot td:first-child{{text-align:left;white-space:nowrap}}
.ht td.z{{color:rgba(128,128,128,.5)}}
.ht td.h{{position:relative;cursor:help}}
.ht td.h .n{{color:rgb(28,131,225);font-weight:700;border-bottom:1px dashed rgb(28,131,225)}}
.ht td.h.w .n{{color:rgb(240,128,30);border-bottom-color:rgb(240,128,30)}}
.ht td:last-child{{background:rgba(255,193,7,.22);font-weight:700}}
.ht tr.tot td{{background:rgba(62,180,137,.18);font-weight:700}}
.ht tr.we td{{background:rgba(128,128,128,.12)}}
.ht tr.we td:last-child{{background:rgba(255,193,7,.30)}}
.ht tr.wk td{{border-top:3px solid rgba(62,180,137,.75)}}
.ht .pop{{display:none;position:absolute;z-index:999;top:100%;min-width:280px;max-width:420px;text-align:left;
  background:#fff;color:#222;border:1px solid #ccc;border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.18);
  padding:8px 12px;font-weight:400;white-space:normal}}
.ht .pop.l{{left:0}} .ht .pop.r{{right:0}}
.ht td.h:hover .pop{{display:block}}
.ht .pop ul{{margin:0;padding-left:16px}} .ht .pop li{{margin:3px 0}} .ht .pop .t{{color:#888}} .ht .pop .m{{color:#888;list-style:none}}
.ht .pop a{{color:rgb(28,131,225)}}
</style><table class="ht"><thead><tr>{head}</tr></thead><tbody>{tot}{"".join(body)}</tbody></table>""")


def page_trends():
    calls_, mts_ = data["calls"], mt
    n_days = max(1, (min(d_end, now.date()) - d_start).days + 1)
    rows = []
    for s_ in list(lb["sale"]) + ["TỔNG"]:
        c_ = calls_ if s_ == "TỔNG" else calls_[calls_["sale"] == s_]
        m_ = mts_ if s_ == "TỔNG" else mts_[mts_["sale"] == s_]
        talk = float(c_["duration"].sum()) if len(c_) else 0.0
        if len(c_):
            per_day = pd.to_datetime(c_["ts"]).dt.tz_convert(TZ).dt.date.value_counts()
            top = f"{per_day.idxmax():%d/%m} ({int(per_day.max())})"
        else:
            top = "—"
        rows.append({"Sale": s_, "Cuộc gọi": len(c_), "Thời lượng gọi": fmt_duration(talk),
                     "TB phút/cuộc": round(talk / 60 / len(c_), 1) if len(c_) else 0.0,
                     "TB cuộc gọi/ngày": round(len(c_) / n_days, 1), "Ngày gọi nhiều nhất": top,
                     "Meeting": len(m_), "TB meeting/tuần": round(len(m_) / n_days * 7, 1)})
    tb = pd.DataFrame(rows)
    if len(tb) == 2:      # chỉ 1 sale → bỏ dòng TỔNG trùng
        tb = tb.iloc[:1]
    st.markdown(f"**📋 Số liệu hoạt động** · {n_days} ngày ({d_start:%d/%m} – {min(d_end, now.date()):%d/%m})")
    st.dataframe(tb.style.apply(lambda r: ["font-weight: bold; background-color: rgba(62, 180, 137, 0.18)"] * len(r)
                                if r["Sale"] == "TỔNG" else [""] * len(r), axis=1)
                 ,
                 hide_index=True, width="stretch", column_config={
                     "Sale": st.column_config.TextColumn(pinned=True),
                     "TB phút/cuộc": st.column_config.NumberColumn(format="%.1f"),
                     "TB cuộc gọi/ngày": st.column_config.NumberColumn(format="%.1f",
                                                                       help=f"Số cuộc gọi ÷ {n_days} ngày trong kỳ"),
                     "TB meeting/tuần": st.column_config.NumberColumn(format="%.1f", help="Số meeting ÷ số ngày × 7"),
                 })
    with st.expander("📅 Số liệu theo từng ngày, từng Sale", expanded=True):
        fmt_ = "DD/MM HH:mm" if d_start == d_end else "DD/MM/YYYY"
        sales_ = list(lb["sale"])

        def by_day(df_):
            base = trend(df_)
            out = pd.DataFrame({"Ngày": base["ts"].dt.tz_localize(None)})
            for s_ in sales_:
                out[s_] = trend(df_[df_["sale"] == s_])["n"].values if len(df_) else 0
            out["Tổng"] = base["n"].values
            return out.sort_values("Ngày", ascending=False)

        def show(tbl):
            num_ = sales_ + ["Tổng"]
            tbl = tbl.copy()
            tbl["Ngày"] = tbl["Ngày"].map(lambda x: day_label(x, d_start == d_end))
            tot = pd.DataFrame([{"Ngày": "TỔNG", **{c: int(tbl[c].sum()) for c in num_}}])
            tbl = pd.concat([tot, tbl], ignore_index=True)       # dòng tổng theo từng Sale ở trên cùng
            st.dataframe(tbl.style.set_properties(subset=["Tổng"], **{"background-color": "rgba(255, 193, 7, 0.22)",
                                                                       "font-weight": "bold"})
                         .map(lambda v: "color: rgba(128, 128, 128, 0.5);" if v == 0 else "", subset=sales_)
                         .apply(lambda r: ["font-weight: bold; background-color: rgba(62, 180, 137, 0.18)"] * len(r)
                                if r["Ngày"] == "TỔNG" else
                                ["background-color: rgba(128, 128, 128, 0.14)"] * len(r)
                                if str(r["Ngày"]).startswith(("T7", "CN")) and d_start != d_end else [""] * len(r),
                                axis=1),
                         hide_index=True, width="stretch",
                         column_config={"Ngày": st.column_config.TextColumn(pinned=True)})

        t1, t2, t3 = st.tabs(["📞 Cuộc gọi", "🤝 Deal mới", "📅 Meeting"])
        with t1:
            show(by_day(calls_))
        nd = data.get("new_deal_list", pd.DataFrame(columns=NEW_DEAL_COLS))
        has_titles = not nd.empty or data["deals_new"].empty
        with t2:
            if has_titles:
                st.caption("Rê chuột vào ô số để xem danh sách deal (bấm tên để mở deal trên Bitrix24).")
                _hover_day_table(nd, sales_, lambda r: _link_html(r.title or "(không tên)", r.link))
            else:
                st.caption("Rê chuột để xem tên deal cần dữ liệu mới: leader cài lại nút 📥 Cập nhật Bitrix.")
                show(by_day(data["deals_new"]))
        with t3:
            st.markdown("Rê chuột vào ô số để xem danh sách meeting · "
                        "<span style='color:rgb(240,128,30);font-weight:700'>số màu cam</span> = có meeting "
                        "**thiếu recap**.", unsafe_allow_html=True)
            _hover_day_table(mts_, sales_, lambda r: f"{escape(str(r.title))} · <i>{escape(str(r.status))}</i>",
                             warn=lambda sub: bool((sub["status"] == ST_MISS).any()))
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📞 Số cuộc gọi**")
        st.plotly_chart(trend_chart(trend(data["calls"]), COLOR_CALLS, "Cuộc gọi"), width="stretch")
    with c2:
        st.markdown("**📅 Số meeting**")
        st.plotly_chart(trend_chart(trend(mt), COLOR_MEETINGS, "Meeting"), width="stretch")


def _render_won_breakdown():
    """Bảng doanh thu/số deal won theo Source và theo Gói dịch vụ (trang Hiệu suất theo Sale)."""
    w = data.get("won_list", pd.DataFrame(columns=WON_LIST_COLS))
    _snap = globals().get("snap")
    st.divider()
    st.markdown("**II. Kết quả kinh doanh theo Source**")
    if _snap is not None and "dwon" not in _snap:
        st.info("Cần dữ liệu mới: leader cài lại nút **📥 Cập nhật Bitrix** rồi bấm trên Bitrix24.")
        return
    if w.empty:
        st.caption("Không có deal won nào trong kỳ.")
        return
    team = secret("haracrm", "team", "S2.4")
    sales_rows = [s_ for s_ in lb["sale"] if s_ in set(w["sale"])]
    _won_pivot(w.assign(grp=w["source"].map(source_group)), "grp", SOURCE_GROUP_ORDER, sales_rows, team)
    st.caption("PU = số deal won · RE = doanh thu. STT = Sale tự tìm · HOTLINE/CHAT = Chat, Phone · "
               "PARTNER = Partner giới thiệu · UP SELL = Upsell · MKT = tất cả source còn lại.")
    st.markdown("**III. Kết quả kinh doanh theo Gói dịch vụ**")
    pk = (_snap or {}).get("pkgField")
    if _snap is not None and not pk:
        st.warning("Không tìm thấy trường **Gói dịch vụ** của deal trên Bitrix24. Điền "
                   '`package_field = "Tên trường"` vào mục `[bitrix24]` trong Secrets rồi cài lại nút Cập nhật Bitrix.')
        return
    pk_order = list(w.groupby("package")["amount"].sum().sort_values(ascending=False).index)
    pk_order = [p_ for p_ in pk_order if p_ != "(Chưa chọn gói)"] + [p_ for p_ in pk_order if p_ == "(Chưa chọn gói)"]
    _won_pivot(w, "package", pk_order, sales_rows, team)
    if pk:
        st.caption(f"Theo trường **{pk[1]}** trên deal.")


def _won_pivot(w: pd.DataFrame, col: str, order: list, sales_rows: list, team: str):
    """Bảng PU (số deal won) / RE (doanh thu) theo nhóm, tiêu đề nhóm gộp ô; dòng đầu là tổng team."""
    from html import escape
    fmt = lambda v: f"{v:,.0f}".replace(",", ".")
    groups = [("TỔNG", "All RE")] + [(g, "RE") for g in order]

    pu_ = lambda d: int(d["pu"].fillna(1).sum()) if "pu" in d else len(d)

    def cells(df_):
        out = [(pu_(df_), df_["amount"].sum(), True)]
        for g in order:
            sub = df_[df_[col] == g]
            out.append((pu_(sub), sub["amount"].sum(), False))
        return out

    head1 = '<th rowspan="2" class="s">Sale</th>' + "".join(
        f'<th colspan="2" class="g{" t" if i_ == 0 else ""}">{escape(str(g))}</th>' for i_, (g, _) in enumerate(groups))
    head2 = "".join(f'<th class="{"t" if i_ == 0 else ""}">PU</th><th class="{"t" if i_ == 0 else ""}">{re_}</th>'
                    for i_, (_, re_) in enumerate(groups))
    body = []
    for label, df_, cls in [(f"{team} (tổng)", w, "team")] + [(s_, w[w["sale"] == s_], "") for s_ in sales_rows]:
        tds = "".join(f'<td class="n{" t" if tot_ else ""}">{fmt(pu)}</td><td class="n{" t" if tot_ else ""}">{fmt(re_)}</td>'
                      for pu, re_, tot_ in cells(df_))
        body.append(f'<tr class="{cls}"><td class="s">{escape(label)}</td>{tds}</tr>')
    st.html(f"""<style>
.wp{{border-collapse:collapse;width:100%;font-size:14px;font-family:inherit}}
.wp th,.wp td{{border:1px solid rgba(128,128,128,.28);padding:6px 10px}}
.wp th{{background:rgba(62,180,137,.18);font-weight:700;text-align:center;white-space:normal}}
.wp th.t,.wp td.t{{background:rgba(255,193,7,.22);font-weight:700}}
.wp td.n{{text-align:right;white-space:nowrap}}
.wp td.s{{white-space:nowrap}}
.wp tr.team td{{background:rgba(62,180,137,.12);font-weight:700}}
.wp tr.team td.t{{background:rgba(255,193,7,.32)}}
</style><div style="overflow-x:auto"><table class="wp"><thead><tr>{head1}</tr><tr>{head2}</tr></thead>
<tbody>{"".join(body)}</tbody></table></div>""")


def page_won_deals():
    w = data.get("won_list", pd.DataFrame(columns=WON_LIST_COLS))
    _snap = globals().get("snap")
    if _snap is not None and "dwon" not in _snap:
        st.warning("Cần dữ liệu mới: leader cài lại nút **📥 Cập nhật Bitrix** (trang Cập nhật Bitrix) rồi bấm trên Bitrix24.")
        return
    rf = (_snap or {}).get("revField")
    c1, c2 = st.columns([1, 1.4])
    s_pick = c1.selectbox("🔍 Lọc theo Sale", ["Tất cả"] + list(lb["sale"]), key="won_sale_filter")
    src_all = sorted(w["source"].astype(str).unique(), key=_norm_txt) if not w.empty else []
    src_pick = c2.multiselect("🏷️ Lọc theo Source", src_all, placeholder="Tất cả source", key="won_src_filter")
    if s_pick != "Tất cả":
        w = w[w["sale"] == s_pick]
    if src_pick:
        w = w[w["source"].astype(str).isin(src_pick)]
    _won_bitrix = w[w["id"].astype(str) != ""].copy()
    if "bitrix_amount" not in _won_bitrix:
        _won_bitrix["bitrix_amount"] = _won_bitrix["amount"]
    _won_bitrix["bitrix_amount"] = _won_bitrix["bitrix_amount"].fillna(_won_bitrix["amount"])
    k = st.columns(3)
    n_won = int(w["pu"].fillna(1).sum()) if "pu" in w else len(w)
    k[0].metric("🏆 Số deal won", fmt_int(n_won))
    k[1].metric("💰 Tổng tiền", fmt_vnd(w["amount"].sum()))
    k[2].metric("📊 Trung bình / deal", fmt_vnd(w["amount"].sum() / n_won) if n_won > 0 else "—")
    st.caption(f"Deal Won có **{rf[1] if rf else 'ngày Won'}** trong kỳ {d_start:%d/%m/%Y} – {d_end:%d/%m/%Y}. "
               "Bấm **Mở deal ↗** để xem deal trên Bitrix24.")
    if w.empty:
        st.info("Không có deal won nào với bộ lọc hiện tại.")
        _render_won_edit(_won_bitrix)
        return
    w = w.copy()
    if "bitrix_amount" in w:
        edited_ = w["bitrix_amount"].notna() & (w["bitrix_amount"] != w["amount"])
        w.loc[edited_, "title"] = "✏️ " + w.loc[edited_, "title"].astype(str)
        w["Ghi chú"] = [(f"Bitrix ghi {b_:,.0f}".replace(",", ".") + (f" · {n_}" if isinstance(n_, str) and n_ else ""))
                        if e_ else "" for b_, n_, e_ in zip(w["bitrix_amount"], w["fixed_note"] if "fixed_note" in w else [""] * len(w), edited_)]
    else:
        w["Ghi chú"] = ""
    w.loc[w["id"].astype(str) == "", "Ghi chú"] = "Thêm tay (ngoài Bitrix)"
    view = (w.sort_values(["day", "amount"], ascending=[False, False])
            .rename(columns={"day": "Ngày ghi nhận", "sale": "Sale", "title": "Tên deal", "amount": "Số tiền (₫)",
                             "source": "Source", "link": "Link deal"})
            [["Ngày ghi nhận", "Sale", "Tên deal", "Số tiền (₫)", "Source", "Ghi chú", "Link deal"]])
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "Ngày ghi nhận": st.column_config.DateColumn(format="DD/MM/YYYY"),
        "Tên deal": st.column_config.TextColumn(width="large"),
        "Số tiền (₫)": st.column_config.NumberColumn(format="localized"),
        "Link deal": st.column_config.LinkColumn(display_text="Mở deal ↗"),
    })
    st.download_button("⬇️ Tải danh sách deal won (CSV)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"deal_won_{d_start:%Y%m%d}_{d_end:%Y%m%d}.csv", mime="text/csv")
    _render_won_edit(_won_bitrix)


def page_google_connect():
    c = dict(secret("google_oauth") or {})
    st.markdown(
        "Trang dành cho **leader**: đăng nhập Google 1 lần để dashboard đọc lịch (meeting) và hộp thư "
        "(email recap mà Sale CC cho leader). Quyền chỉ đọc. Có thể thu hồi bất cứ lúc nào tại "
        "[myaccount.google.com/permissions](https://myaccount.google.com/permissions).")
    if leader_mode():
        st.success(f"Đang kết nối với hộp thư của leader {c.get('leader_email', '')}. "
                   "Chỉ cần làm lại các bước dưới nếu dashboard báo lỗi `invalid_grant` (mất kết nối).")
    st.markdown("**Bước 1.** Bấm nút, **chọn tài khoản email công ty của leader** (không chọn gmail cá nhân), tick đủ 2 quyền, bấm **Continue / Tiếp tục**.")
    st.link_button("🔑 Đăng nhập Google (leader)", oauth_login_url(), type="primary")
    st.markdown("**Bước 2.** Google chuyển tới một trang báo lỗi *\"This site can't be reached\"* — "
                "**đó là bình thường**. Copy toàn bộ địa chỉ trên thanh địa chỉ (bắt đầu bằng `http://localhost`) "
                "và dán vào ô dưới:")
    pasted = st.text_input("Địa chỉ trang lỗi", placeholder="http://localhost/?code=4/0Ab...&scope=...",
                           label_visibility="collapsed")
    code = extract_code(pasted) if pasted else st.query_params.get("code", "")
    if not code:
        return
    cache = st.session_state.setdefault("oauth_results", {})
    if code not in cache:  # mỗi mã chỉ đổi được 1 lần
        try:
            cache[code] = oauth_exchange(code)
        except Exception as e:
            cache[code] = {"error": type(e).__name__, "error_description": str(e)}
    tok = cache[code]
    if "refresh_token" not in tok:
        st.error(f"Không lấy được mã kết nối: {tok.get('error_description') or tok.get('error') or tok}. "
                 "Hãy bấm lại nút ở Bước 1 để lấy địa chỉ mới (mỗi địa chỉ chỉ dùng được 1 lần, trong vài phút).")
        return
    if c.get("leader_email") and tok.get("email", "").lower() != str(c["leader_email"]).lower():
        st.warning(f"Bạn vừa đăng nhập bằng **{tok.get('email')}**, khác với leader_email "
                   f"**{c['leader_email']}** trong Secrets. Nếu nhầm tài khoản, hãy làm lại Bước 1.")
    st.markdown(f"**Bước 3.** Đã kết nối tài khoản **{tok.get('email')}**. Copy dòng dưới, mở "
                "**Manage app → ⋮ → Settings → Secrets**, thay dòng `refresh_token` trong phần "
                "`[google_oauth]` rồi bấm **Save**:")
    st.code(f'refresh_token = "{tok["refresh_token"]}"', language="toml")
    st.caption("Mã này cho phép đọc lịch và hộp thư của leader — chỉ dán vào Secrets, không gửi qua chat/email.")


HANDOFF_HTML = """<div id=m style="font:13px sans-serif;color:rgb(90,90,90)">⏳ Đang chờ dữ liệu từ tab __SRC__…</div><script>
const P=window.parent;let done=false;
const ping=()=>{if(done)return;for(const o of [window.top.opener,P.opener]){try{if(o)o.postMessage('bx-ready','*');}catch(e){}}};
window.addEventListener('message',ev=>{const d=ev.data;if(done||!d||!d.bx)return;done=true;
const doc=P.document;const ta=doc.querySelector('textarea');if(!ta){done=false;return;}
Object.getOwnPropertyDescriptor(P.HTMLTextAreaElement.prototype,'value').set.call(ta,d.bx);
ta.dispatchEvent(new P.Event('input',{bubbles:true}));
document.getElementById('m').innerText='✅ Đã nhận dữ liệu từ __SRC__, đang lưu…';
setTimeout(()=>{const b=[...doc.querySelectorAll('button')].find(x=>x.innerText.includes('Lưu dữ liệu'));if(b)b.click();try{ev.source.postMessage('bx-saved','*');}catch(e){}},600);});
ping();const iv=setInterval(ping,700);setTimeout(()=>{clearInterval(iv);if(!done)document.getElementById('m').innerText='Không tự nhận được — dán tay theo hướng dẫn dưới.';},60000);
</script>"""


def page_hara_update():
    import streamlit.components.v1 as components
    st.markdown("Dashboard lấy các chỉ số funnel **đã tính sẵn trên HaraCRM** (salemaxcr.haracrm.com): leader mở "
                "dashboard HaraCRM, chọn bộ lọc kỳ muốn xem, rồi bấm nút **📈 Cập nhật HaraCRM** trên thanh dấu trang. "
                f"Nút chỉ lấy dòng của team **{secret('haracrm', 'team', 'S2.4')}** và các nhân viên trong team.")
    incoming = st.query_params.get("hx", "")
    paste_mode = st.query_params.get("paste", "") == "1"
    if paste_mode:
        components.html(HANDOFF_HTML.replace("__SRC__", "HaraCRM"), height=28)
    pasted = ""
    with st.expander("📋 Dán dữ liệu từ nút Cập nhật HaraCRM", expanded=paste_mode):
        with st.form("hx_paste_form", clear_on_submit=True, border=False):
            txt = st.text_area("Dữ liệu (nút đã tự copy sẵn — chỉ cần dán)", height=90)
            if st.form_submit_button("💾 Lưu dữ liệu", type="primary"):
                pasted = txt
    raw = incoming or pasted
    if raw:
        try:
            snap_h = decode_payload(raw, "hx")
            if snap_h.get("kind") != "hara":
                raise ValueError("Đây không phải dữ liệu của nút Cập nhật HaraCRM.")
            where, err = save_snapshot(snap_h, HARA_SNAP)
            st.session_state["hx_last"] = (snap_h, where, err)
            st.cache_data.clear()
        except Exception as e:
            st.error(f"Không đọc được dữ liệu: {e}")
        if incoming or paste_mode:
            st.query_params.clear()
            st.rerun()
    cur = st.session_state["hx_last"][0] if "hx_last" in st.session_state else get_snapshot(HARA_SNAP)[0]
    if "hx_last" in st.session_state:
        h, where, err = st.session_state["hx_last"]
        at_ = datetime.fromtimestamp(h["at"] / 1000, TZ)
        per_txt = " · ".join(f"{p_['label']}: {('⚠️ ' + p_['err']) if p_.get('err') else str(len(p_.get('tables', []))) + ' bảng'}"
                             for p_ in h.get("periods", [])) or f"{len(h.get('tables', []))} bảng"
        st.success(f"✅ Đã nhận dữ liệu HaraCRM lúc {at_:%H:%M %d/%m} ({per_txt}) — lưu tại **{where}**.")
        if err:
            st.warning(f"Chưa lưu được vào Google Drive nên dữ liệu chỉ giữ tạm. Chi tiết: `{err}`")
        if h.get("missTabs"):
            st.warning(f"Không tìm thấy tab {h['missTabs']} trên trang HaraCRM.")
        st.page_link(pages["Tổng quan"][1], label="→ Xem Chỉ số funnel HaraCRM", icon="📊")
    elif cur:
        at_ = datetime.fromtimestamp(cur.get("at", 0) / 1000, TZ)
        st.caption(f"Dữ liệu đang dùng: gửi lúc {at_:%H:%M %d/%m}, kỳ trên HaraCRM: {cur.get('filter') or 'không rõ'}.")
    if cur and cur.get("bv") != HARA_VERSION:
        st.warning("⚠️ Dữ liệu đến từ nút cũ — xóa nút cũ, kéo nút mới bên dưới vào rồi bấm lại.")

    st.divider()
    st.subheader("Cài nút 📈 Cập nhật HaraCRM (làm 1 lần)")
    base = secret("bitrix24", "dashboard_url") or ""
    if not base:
        try:
            from urllib.parse import urlparse
            u = urlparse(st.context.url)
            base = f"{u.scheme}://{u.netloc}"
        except Exception:
            base = ""
    href = hara_href(base.rstrip("/") + "/cap-nhat-haracrm", roster)
    st.markdown("**Cách 1 — kéo thả:** hiện thanh dấu trang (`Cmd/Ctrl + Shift + B`), rồi **kéo nút xanh dưới đây** "
                "thả lên thanh dấu trang.")
    components.html(
        f"""<a href="{href}" onclick="alert('Đừng bấm ở đây — hãy KÉO nút này lên thanh dấu trang, rồi bấm nó khi đang mở dashboard HaraCRM.');return false;"
        style="display:inline-block;padding:10px 18px;background:rgb(42,120,214);color:white;border-radius:8px;
        font:600 15px sans-serif;text-decoration:none;cursor:grab">📈 Cập nhật HaraCRM</a>""", height=56)
    st.markdown("**Cách 2 — tạo tay:** chuột phải thanh dấu trang → **Thêm trang… / Add page…** → Tên: "
                "`📈 Cập nhật HaraCRM` → ô **URL** dán toàn bộ đoạn dưới → Lưu.")
    st.code(href, language=None, wrap_lines=True)
    tabs_ = list(secret("haracrm", "tabs", ["Điều hành", "Năng lực NV"]))
    st.markdown("**Mỗi lần cập nhật:** mở `salemaxcr.haracrm.com/dashboard` → chọn **Vùng / Team** muốn xem → "
                f"bấm **📈 Cập nhật HaraCRM**. Nút tự bấm **tháng trước** rồi **tháng này** (nút T10, T11…), mỗi tháng "
                f"mở lần lượt các tab {', '.join(tabs_)} và đọc các bảng có dòng của team, rồi hiện nút "
                "**📤 Gửi sang dashboard** (mất khoảng 30 giây – 1 phút).")


def page_vc_update():
    import streamlit.components.v1 as components
    st.markdown("Số **cuộc gọi đi** lấy thẳng từ VoiceCloud (portal.voicecloud.vn → **Report → Outgoings**), đếm theo "
                "máy lẻ (Caller ID) của từng Sale. Dữ liệu lưu vào Google Drive của leader.")
    emap = vc_ext_map(roster)
    st.caption("Máy lẻ: " + (" · ".join(f"**{k}** {v}" for k, v in emap.items()) or "chưa khớp được Sale nào")
               + " — sửa trong Secrets: mục `[voicecloud_portal]` → `extensions = { \"1226\" = \"Hiếu\", … }`, "
                 "hoặc `voicecloud_id` của từng Sale.")
    incoming = st.query_params.get("vx", "")
    paste_mode = st.query_params.get("paste", "") == "1"
    if paste_mode:
        components.html(HANDOFF_HTML.replace("__SRC__", "VoiceCloud"), height=28)
    pasted = ""
    with st.expander("📋 Dán dữ liệu từ nút Cập nhật VoiceCloud", expanded=paste_mode):
        with st.form("vx_paste_form", clear_on_submit=True, border=False):
            txt = st.text_area("Dữ liệu (nút đã tự copy sẵn — chỉ cần dán)", height=90)
            if st.form_submit_button("💾 Lưu dữ liệu", type="primary"):
                pasted = txt
    raw = incoming or pasted
    if raw:
        try:
            snap_v = decode_payload(raw, "vx")
            if snap_v.get("kind") != "vc":
                raise ValueError("Đây không phải dữ liệu của nút Cập nhật VoiceCloud.")
            where, err = save_snapshot(snap_v, VC_SNAP)
            st.session_state["vx_last"] = (snap_v, where, err)
            st.cache_data.clear()
        except Exception as e:
            st.error(f"Không đọc được dữ liệu: {e}")
        if incoming or paste_mode:
            st.query_params.clear()
            st.rerun()
    cur = st.session_state["vx_last"][0] if "vx_last" in st.session_state else get_snapshot(VC_SNAP)[0]
    if "vx_last" in st.session_state:
        v_, where, err = st.session_state["vx_last"]
        at_ = datetime.fromtimestamp(v_["at"] / 1000, TZ)
        per_ = " · ".join(f"{k} {sum(x[0] for x in m.values())}" for k, m in (v_.get("calls") or {}).items())
        st.success(f"✅ Đã nhận {v_.get('rows', 0)} cuộc gọi đi ({str(v_.get('from'))[:10]} → {str(v_.get('to'))[:10]}) "
                   f"lúc {at_:%H:%M %d/%m} — lưu tại **{where}**. {per_}")
        if err:
            st.warning(f"Chưa lưu được vào Google Drive nên dữ liệu chỉ giữ tạm. Chi tiết: `{err}`")
    elif cur:
        at_ = datetime.fromtimestamp(cur.get("at", 0) / 1000, TZ)
        st.caption(f"Dữ liệu đang dùng: gửi lúc {at_:%H:%M %d/%m}, {cur.get('rows', 0)} cuộc gọi đi, "
                   f"{str(cur.get('from'))[:10]} → {str(cur.get('to'))[:10]}.")
    if cur and cur.get("bv") != VC_VERSION:
        st.warning("⚠️ Dữ liệu đến từ nút cũ — xóa nút cũ, kéo nút mới bên dưới vào rồi bấm lại.")
    if cur and cur.get("other"):
        top_ = sorted(cur["other"].items(), key=lambda kv: -kv[1])[:8]
        st.caption("Máy lẻ khác (không tính): " + " · ".join(f"{k or '—'}: {v}" for k, v in top_))

    st.divider()
    st.subheader("Cài nút 📞 Cập nhật VoiceCloud (làm 1 lần)")
    base = secret("bitrix24", "dashboard_url") or ""
    if not base:
        try:
            from urllib.parse import urlparse
            u = urlparse(st.context.url)
            base = f"{u.scheme}://{u.netloc}"
        except Exception:
            base = ""
    href = vc_href(base.rstrip("/") + "/cap-nhat-voicecloud", roster)
    st.markdown("**Cách 1 — kéo thả:** hiện thanh dấu trang (`Cmd/Ctrl + Shift + B`), rồi **kéo nút xanh dưới đây** "
                "thả lên thanh dấu trang.")
    components.html(
        f"""<a href="{href}" onclick="alert('Đừng bấm ở đây — hãy KÉO nút này lên thanh dấu trang, rồi bấm nó khi đang mở VoiceCloud Report → Outgoings.');return false;"
        style="display:inline-block;padding:10px 18px;background:rgb(42,120,214);color:white;border-radius:8px;
        font:600 15px sans-serif;text-decoration:none;cursor:grab">📞 Cập nhật VoiceCloud</a>""", height=56)
    st.markdown("**Cách 2 — tạo tay:** chuột phải thanh dấu trang → **Thêm trang… / Add page…** → Tên: "
                "`📞 Cập nhật VoiceCloud` → ô **URL** dán toàn bộ đoạn dưới → Lưu.")
    st.code(href, language=None, wrap_lines=True)
    st.markdown("**Mỗi lần cập nhật:** đăng nhập `portal.voicecloud.vn` → **Report → Outgoings** → bấm "
                "**📞 Cập nhật VoiceCloud**. Nút tự đặt **From = đầu tháng trước, To = hôm nay** và bấm 🔍 "
                "(nếu trang tải lại thì bấm nút thêm 1 lần), rồi đọc hết các trang danh sách và hiện nút "
                "**📤 Gửi sang dashboard**. Extension / Status để **All**.")


def page_bitrix_update():
    import streamlit.components.v1 as components
    st.markdown("Công ty không cho tạo webhook, nên dashboard đọc Bitrix24 **bằng phiên đăng nhập của leader**: "
                "leader bấm một nút trên thanh dấu trang khi đang mở Bitrix24. Dữ liệu (3 tháng gần nhất) được "
                "lưu vào Google Drive của leader để cả team cùng xem.")
    # 1) Nhận dữ liệu gửi sang (qua ?bx=... hoặc dán tay)
    incoming = st.query_params.get("bx", "")
    paste_mode = st.query_params.get("paste", "") == "1"
    if paste_mode:
        components.html(HANDOFF_HTML.replace("__SRC__", "Bitrix24"), height=28)
        st.info("📋 Dữ liệu Bitrix24 đã được **copy sẵn**. Bấm vào ô dưới, nhấn **Cmd/Ctrl + V** rồi bấm **💾 Lưu dữ liệu**.")
    pasted = ""
    with st.expander("📋 Dán dữ liệu từ nút Cập nhật Bitrix", expanded=paste_mode):
        with st.form("bx_paste_form", clear_on_submit=True, border=False):
            txt = st.text_area("Dữ liệu (nút đã tự copy sẵn — chỉ cần dán)", height=90)
            if st.form_submit_button("💾 Lưu dữ liệu", type="primary"):
                pasted = txt
    raw = incoming or pasted
    if raw:
        try:
            snap = decode_snapshot(raw)
            where, err = save_snapshot(snap)
            st.session_state["bx_last"] = (snap, where, err)
            st.cache_data.clear()
        except Exception as e:
            st.error(f"Không đọc được dữ liệu: {e}")
        if incoming or paste_mode:
            st.query_params.clear()
            st.rerun()
    cur = (st.session_state["bx_last"][0] if "bx_last" in st.session_state
           else (get_snapshot()[0] if bitrix_mode() == "bookmarklet" else None))
    if cur and cur.get("bv") != BOOKMARKLET_VERSION:
        cur_at = datetime.fromtimestamp(cur.get("at", 0) / 1000, TZ)
        st.warning(f"⚠️ Dữ liệu đang dùng (gửi lúc **{cur_at:%H:%M %d/%m}**) đến từ **nút cũ** "
                   f"(phiên bản {cur.get('bv') or cur.get('v', 1)}, mới nhất là {BOOKMARKLET_VERSION}). "
                   "Xóa nút cũ trên thanh dấu trang, kéo nút mới ở bước 1 bên dưới vào, rồi bấm lại trên Bitrix24.")
    elif cur:
        cur_at = datetime.fromtimestamp(cur.get("at", 0) / 1000, TZ)
        st.caption(f"Dữ liệu đang dùng: gửi lúc {cur_at:%H:%M %d/%m} bằng nút mới nhất (phiên bản {BOOKMARKLET_VERSION}).")
    if "bx_last" in st.session_state:
        snap, where, err = st.session_state["bx_last"]
        at = datetime.fromtimestamp(snap["at"] / 1000, TZ)
        st.success(f"✅ Đã nhận dữ liệu Bitrix24 lúc {at:%H:%M %d/%m/%Y} (từ {snap['from']}) — lưu tại **{where}**.")
        for w_ in snap.get("warn") or []:
            st.warning(f"Bitrix24 báo lỗi một phần: {w_}")
        if err:
            st.warning("Chưa lưu được vào Google Drive nên dữ liệu chỉ giữ tạm tới khi app khởi động lại. "
                       f"Chi tiết: `{err}`. Cách sửa: bật **Google Drive API** trong Google Cloud và vào "
                       "**🔑 Kết nối Google** đăng nhập lại (tick đủ 3 quyền).")
        have = [str(x).strip().lower() for x in snap.get("allStages", [])]
        missing = [x for x in lead_stage_config()[0] if have and x.strip().lower() not in have]
        if missing:
            st.warning(f"Không thấy stage {missing} trên Bitrix24. Stage hiện có: {snap.get('allStages')}. "
                       "Sửa dòng `lead_stages` trong Secrets cho đúng tên.")
        rows, users = [], snap.get("users", {})
        for r in roster.itertuples():
            if snap.get("v", 1) >= 2:   # bản mới: dữ liệu theo tên Sale, tài khoản tìm theo email/tên
                key = r.sale
                uid, how = users.get(r.sale, ["", ""])
            else:                       # bản cũ: dữ liệu theo bitrix_id
                key = clean_id(r.bitrix_id)
                uid, how = (key, "bitrix_id") if key else ("", "")
            days = snap["agg"].get(key, {}) if key else {}
            v = np.array(list(days.values()) or [[0, 0, 0, 0]], dtype=float).sum(axis=0)
            rows.append({"Sale": r.sale,
                         "Tài khoản Bitrix": f"ID {uid} (tìm theo {how})" if uid else "❌ Không tìm thấy",
                         "Lead mới": int(v[0]), "Deal mới": int(v[1]), "Deal won": int(v[2]), "Doanh thu (₫)": v[3],
                         "Lead theo stage": sum(snap["stages"].get(key, {}).values()) if key else 0})
        summary = pd.DataFrame(rows)
        st.dataframe(summary, hide_index=True, width="stretch",
                     column_config={"Doanh thu (₫)": st.column_config.NumberColumn(format="localized")})
        if snap.get("missingSources"):
            st.warning(f"Không thấy nguồn lead {snap['missingSources']} trên Bitrix24 nên chưa loại được khỏi Lead tồn. "
                       f"Nguồn hiện có: {snap.get('allSources')}. Sửa dòng `lead_exclude_sources` trong Secrets "
                       "cho đúng tên, rồi cài lại nút.")
        if snap.get("v", 0) == 4:
            if snap.get("reasonField"):
                st.caption(f"Lý do Unqualified lấy từ trường: **{snap['reasonField'][1]}** (`{snap['reasonField'][0]}`).")
            else:
                st.warning("Không tìm thấy trường lý do Unqualified. Điền `unqual_reason_field = \"Tên trường\"` vào mục "
                           f"`[bitrix24]` trong Secrets rồi cài lại nút. Trường tự tạo hiện có: {snap.get('reasonCands')}")
        if snap.get("v", 0) >= 5:
            if snap.get("revField"):
                st.caption(f"Doanh thu tính theo trường: **{snap['revField'][1]}** (`{snap['revField'][0]}`).")
            else:
                st.warning("Không tìm thấy trường **Ngày ghi nhận doanh số** của deal nên Doanh thu và Deal won đang tính theo ngày Won. "
                           "Điền `revenue_date_field = \"Tên trường\"` vào mục `[bitrix24]` trong Secrets rồi cài lại nút. "
                           f"Các trường ngày của deal: {snap.get('revCands')}")
            if snap.get("reasonFields"):
                st.caption("Lý do Unqualified lấy từ: " + ", ".join(f"**{l}** (`{c}`)" for c, l in snap["reasonFields"]) + ".")
        if "calls" in snap:
            n_calls = sum(int(n) for m in snap["calls"].values() for n, _ in m.values())
            src_txt = {"telephony": "thống kê tổng đài Bitrix24", "activity": "hoạt động Cuộc gọi trong CRM"}.get(
                snap.get("callSrc"), "")
            if n_calls:
                st.caption(f"📞 Cuộc gọi: {n_calls:,} cuộc".replace(",", ".") + f" — đọc từ {src_txt}.")
            else:
                st.warning("📞 Không đọc được cuộc gọi nào trên Bitrix24 (tài khoản leader có thể không có quyền xem "
                           "thống kê tổng đài). Xem phần lỗi bên trên nếu có.")
        if snap.get("v", 0) >= 4 and "dlost" in snap:
            if snap.get("lostField"):
                st.caption(f"Lý do Lost lấy từ trường: **{snap['lostField'][1]}** (`{snap['lostField'][0]}`).")
            else:
                st.warning("Không tìm thấy trường lý do Lost của deal. Điền `lost_reason_field = \"Tên trường\"` vào mục "
                           f"`[bitrix24]` trong Secrets rồi cài lại nút. Trường tự tạo của deal: {snap.get('lostCands')}")
        if snap.get("missingSources"):
            pass
        elif snap.get("exclSources"):
            st.caption(f"Lead tồn theo stage không tính lead có nguồn: {', '.join(snap['exclSources'])}.")
        if snap.get("unresolved"):
            st.error(f"Không tìm thấy tài khoản Bitrix24 của: **{', '.join(snap['unresolved'])}**. Kiểm tra `email` "
                     "trong Secrets có đúng email đăng nhập Bitrix24 không, hoặc `name` có đúng họ tên trên Bitrix24 không.")
        empty = summary[(summary["Tài khoản Bitrix"] != "❌ Không tìm thấy") &
                        (summary[["Lead mới", "Deal mới", "Deal won", "Lead theo stage"]].sum(axis=1) == 0)]
        if len(empty):
            st.warning(f"Đã tìm thấy tài khoản nhưng không có lead/deal nào (từ {snap['from']}) của: "
                       f"**{', '.join(empty['Sale'])}**. Nếu không đúng thực tế, báo lại để kiểm tra.")
        st.page_link(pages["Tổng quan"][0], label="→ Xem Tổng quan", icon="📊")

    # 2) Cài nút (một lần)
    st.divider()
    st.subheader("Cài nút 📥 Cập nhật Bitrix (làm 1 lần)")
    base = secret("bitrix24", "dashboard_url") or ""
    if not base:
        try:
            from urllib.parse import urlparse
            u = urlparse(st.context.url)
            base = f"{u.scheme}://{u.netloc}"
        except Exception:
            base = ""
    target = base.rstrip("/") + "/cap-nhat-bitrix"
    href = bookmarklet_href(target, roster)
    st.markdown("**Cách 1 — kéo thả:** hiện thanh dấu trang của Chrome (`Cmd/Ctrl + Shift + B`), rồi **kéo nút xanh "
                "dưới đây** thả lên thanh dấu trang.")
    components.html(
        f"""<a href="{href}" onclick="alert('Đừng bấm ở đây — hãy KÉO nút này lên thanh dấu trang, rồi bấm nó khi đang mở Bitrix24.');return false;"
        style="display:inline-block;padding:10px 18px;background:rgb(42,120,214);color:white;border-radius:8px;
        font:600 15px sans-serif;text-decoration:none;cursor:grab">📥 Cập nhật Bitrix</a>""", height=56)
    st.markdown("**Cách 2 — tạo tay** (nếu kéo không được): chuột phải thanh dấu trang → **Thêm trang… / Add page…** → "
                "Tên: `📥 Cập nhật Bitrix` → ô **URL** dán toàn bộ đoạn dưới (bấm biểu tượng copy ở góc phải) → Lưu.")
    st.code(href, language=None, wrap_lines=True)
    st.markdown("**Mỗi lần cập nhật:** mở một trang Bitrix24 bất kỳ → bấm **📥 Cập nhật Bitrix** trên thanh dấu trang → "
                "chờ khung thông báo đọc xong (khoảng 30 giây – 2 phút) → bấm **📤 Gửi sang dashboard**.")
    st.caption(f"Nút đọc dữ liệu của {len(roster)} Sale trong Secrets — tự tìm tài khoản Bitrix24 theo email, "
               "nếu không thấy thì theo họ tên. Khi thêm/bớt Sale trong Secrets, hãy cài lại nút "
               "(xoá dấu trang cũ, kéo nút mới).")


pages = {"Tổng quan": [
    st.Page(page_overview, title="Hiệu suất theo Sale", icon="🏆", default=True),
    st.Page(page_hara, title="Chỉ số funnel HaraCRM", icon="📊", url_path="funnel-haracrm"),
], "Báo cáo": [
    st.Page(page_leads, title="Lead tồn theo stage", icon="🗂️", url_path="lead-ton"),
    st.Page(page_deal_open, title="Deal tồn theo stage", icon="📂", url_path="deal-ton"),
    st.Page(page_meetings, title="Meeting & recap", icon="✉️", url_path="meeting-recap"),
    st.Page(page_trends, title="Xu hướng hoạt động", icon="📈", url_path="xu-huong"),
    st.Page(page_won_deals, title="Danh sách deal won", icon="💰", url_path="deal-won"),
]}
settings = []
if any(st.query_params.get(k_) for k_ in ("bx", "hx", "vx", "paste")):   # tab do nút bookmarklet mở để gửi dữ liệu sang
    _u = str(getattr(st.context, "url", ""))
    st.session_state["recv_page"] = ("vc" if "vx" in st.query_params or "voicecloud" in _u else
                                     "hara" if "hx" in st.query_params or "haracrm" in _u else "bitrix")
if not OWNER and st.session_state.get("recv_page") == "vc":
    settings.append(st.Page(page_vc_update, title="Cập nhật VoiceCloud", icon="📞", url_path="cap-nhat-voicecloud"))
elif not OWNER and st.session_state.get("recv_page") == "hara":
    settings.append(st.Page(page_hara_update, title="Cập nhật HaraCRM", icon="📈", url_path="cap-nhat-haracrm"))
elif not OWNER and st.session_state.get("recv_page") == "bitrix" and not DEMO:
    settings.append(st.Page(page_bitrix_update, title="Cập nhật Bitrix", icon="📥", url_path="cap-nhat-bitrix"))
if OWNER and configured("google_oauth"):
    settings.append(st.Page(page_google_connect, title="Kết nối Google", icon="🔑", url_path="ket-noi-google"))
if OWNER and not DEMO and bitrix_mode() == "bookmarklet":
    settings.append(st.Page(page_bitrix_update, title="Cập nhật Bitrix", icon="📥", url_path="cap-nhat-bitrix"))
if OWNER and not DEMO:
    settings.append(st.Page(page_hara_update, title="Cập nhật HaraCRM", icon="📈", url_path="cap-nhat-haracrm"))
    settings.append(st.Page(page_vc_update, title="Cập nhật VoiceCloud", icon="📞", url_path="cap-nhat-voicecloud"))
if settings:
    pages["Cài đặt"] = settings
pg = st.navigation(pages, position="sidebar", expanded=True)
st.set_page_config(page_title=APP_TITLE)  # giữ tên tab trình duyệt cố định trên mọi trang

st.title(f"{pg.icon} {pg.title}")
who = ", ".join(picked) if picked and len(picked) <= 3 else f"{len(sales_in_view)} Sale"
st.caption(f"Kỳ: **{mode}** · {d_start:%d/%m/%Y} – {d_end:%d/%m/%Y} · **{who}**")
if DEMO:
    st.info("🧪 **Chế độ DEMO** — đang hiển thị dữ liệu giả định.", icon="ℹ️")
if problems:
    with st.expander(f"⚠️ {len(problems)} vấn đề khi tải dữ liệu"):
        for p in problems:
            st.markdown(f"- {p}")

pg.run()

st.divider()
st.caption("Nguồn: Bitrix24 · VoiceCloud · Google Calendar · Gmail")
st.caption("Dashboard created by Khanh Ngoc Ninh")
