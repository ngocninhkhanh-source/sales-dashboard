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
const rmaps={};
for(const k of rfs){const q=lf[k];const mp={};for(const it of (q.items||[])){mp[String(it.ID)]=String(it.VALUE).trim();}
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
const dlost={},dwon={},warn=[];
const closedIds=sts.filter(x=>(C.closed||[]).some(n=>String(x.NAME).trim().toLowerCase()===n.toLowerCase()||String(x.STATUS_ID).toLowerCase()===n.toLowerCase())).map(x=>x.STATUS_ID);
const srcName={},stName={},want=[],ldet={};
for(const x of srcs){srcName[x.STATUS_ID]=String(x.NAME).trim();}
for(const x of sts){stName[x.STATUS_ID]=String(x.NAME).trim();}
for(const n of C.stages){const x=sts.find(x=>String(x.NAME).trim().toLowerCase()===n.toLowerCase()||String(x.STATUS_ID).toLowerCase()===n.toLowerCase());if(x){want.push(x.STATUS_ID);}}
for(const [u,name] of resolved){
const L=await all('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,'>=DATE_CREATE':iso},select:['ID','DATE_CREATE'],order:{ID:'ASC'}},'lead mới của '+name);
L.forEach(x=>add(name,String(x.DATE_CREATE).slice(0,10),0,1));
const D=await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,'>=DATE_CREATE':iso},select:['ID','DATE_CREATE'],order:{ID:'ASC'}},'deal mới của '+name);
D.forEach(x=>add(name,String(x.DATE_CREATE).slice(0,10),1,1));
const W=rdf?[]:await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'S','>=CLOSEDATE':iso},select:['ID','CLOSEDATE','OPPORTUNITY','TITLE','SOURCE_ID'],order:{ID:'ASC'}},'deal won của '+name);dwon[name]=[];const wl=(x,d)=>dwon[name].push([String(x.ID),String(x.TITLE||''),parseFloat(x.OPPORTUNITY)||0,x.SOURCE_ID?(srcName[x.SOURCE_ID]||String(x.SOURCE_ID)):'(Không có nguồn)',d]);
if(!rdf){W.forEach(x=>{const day=String(x.CLOSEDATE).slice(0,10);add(name,day,2,1);add(name,day,3,parseFloat(x.OPPORTUNITY)||0);wl(x,day);});}
if(rdf){const RV=await all('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'S',['>='+rdf]:from},select:['ID','OPPORTUNITY',rdf,'TITLE','SOURCE_ID'],order:{ID:'ASC'}},'doanh thu của '+name);RV.forEach(x=>{const d=String(x[rdf]||'').slice(0,10);if(d){add(name,d,2,1);add(name,d,3,parseFloat(x.OPPORTUNITY)||0);wl(x,d);}});}
stages[name]={};const m={};
if(want.length){let LS;try{LS=await fast('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,STATUS_ID:want,'>=DATE_CREATE':mFrom+'T00:00:00+07:00'},select:['ID','STATUS_ID','SOURCE_ID','DATE_CREATE'].concat(rfs)},'lead theo stage của '+name);}catch(e){if(!rfs.length)throw e;warn.push('Không đọc được trường lý do Unqualified: '+e.message);rfs=[];LS=await fast('crm.lead.list',{filter:{ASSIGNED_BY_ID:u,STATUS_ID:want,'>=DATE_CREATE':mFrom+'T00:00:00+07:00'},select:['ID','STATUS_ID','SOURCE_ID','DATE_CREATE']},'lead theo stage của '+name);}
for(const x of LS){const sn=stName[x.STATUS_ID]||String(x.STATUS_ID);const so=x.SOURCE_ID?(srcName[x.SOURCE_ID]||String(x.SOURCE_ID)):'(Không có nguồn)';let rs='';if(rfs.length&&closedIds.includes(x.STATUS_ID)){rs=rfs.map(k=>{const v=x[k];const arr=(Array.isArray(v)?v:[v]).filter(z=>z!==null&&z!==undefined&&z!==''&&z!==false);return arr.map(z=>rmaps[k][String(z)]||String(z)).join(', ')||'(Chưa ghi lý do)';});}const k=JSON.stringify([sn,so,String(x.DATE_CREATE).slice(0,10),rs]);m[k]=(m[k]||0)+1;if(!excl.includes(x.SOURCE_ID)){stages[name][sn]=(stages[name][sn]||0)+1;}}}
ldet[name]=Object.entries(m).map(([k,v])=>JSON.parse(k).concat([v]));
try{const LD=await fast('crm.deal.list',{filter:{ASSIGNED_BY_ID:u,STAGE_SEMANTIC_ID:'F','>=CLOSEDATE':mFrom+'T00:00:00+07:00'},select:['ID','SOURCE_ID','CLOSEDATE'].concat(lrf?[lrf]:[])},'deal Lost của '+name);
const lm={};for(const x of LD){const so=x.SOURCE_ID?(srcName[x.SOURCE_ID]||String(x.SOURCE_ID)):'(Không có nguồn)';let rs='';if(lrf){const v=x[lrf];const arr=(Array.isArray(v)?v:[v]).filter(z=>z!==null&&z!==undefined&&z!==''&&z!==false);rs=arr.map(z=>lmap[String(z)]||String(z)).join(', ');}const k=JSON.stringify([so,String(x.CLOSEDATE).slice(0,10),rs||'(Chưa ghi lý do)']);lm[k]=(lm[k]||0)+1;}
dlost[name]=Object.entries(lm).map(([k,v])=>JSON.parse(k).concat([v]));}catch(e){dlost[name]=[];warn.push('Deal Lost của '+name+': '+e.message);}
}
const data={v:6,dwon:dwon,bv:C.bv,warn:warn,ldet:ldet,ldetFrom:mFrom,reasonField:rfs.length?[rfs[0],lab(rfs[0])]:null,reasonFields:rfs.map(k=>[k,lab(k)]),reasonMissing:rMissing,revField:rdf?[rdf,dlab(rdf)]:null,revCands:dateF.map(dlab).slice(0,40),reasonCands:cands.map(lab).slice(0,40),dlost:dlost,lostField:lrf?[lrf,dlab(lrf)]:null,lostCands:dcands.map(dlab).slice(0,40),users:found,unresolved:unresolved,exclSources:C.excl||[],missingSources:missingSrc,allSources:srcs.map(x=>x.NAME),at:Date.now(),from:from,portal:location.host,agg:agg,stages:stages,missing:missing,allStages:sts.map(x=>x.NAME)};
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
    cfg = {x.strip().lower(): x for x in lead_stage_config()[0]}  # khớp tên stage không phân biệt hoa thường
    detail = pd.DataFrame(
        [{"sale": key2sale(u), "stage": cfg.get(str(r[0]).strip().lower(), r[0]), "source": r[1], "created": r[2],
          "reason": r[3] if len(r) >= 5 else "", "n": int(r[-1])}
         for u, rows in (snap.get("ldet") or {}).items() if key2sale(u) for r in rows],
        columns=LEAD_DETAIL_COLS)
    portal = snap.get("portal") or ""
    won_list = pd.DataFrame(
        [{"sale": key2sale(u), "id": r[0], "title": r[1], "amount": float(r[2]), "source": r[3],
          "day": r[4], "link": f"https://{portal}/crm/deal/details/{r[0]}/" if portal else ""}
         for u, rows in (snap.get("dwon") or {}).items() if key2sale(u) for r in rows], columns=WON_LIST_COLS)
    won_list["day"] = pd.to_datetime(won_list["day"], errors="coerce").dt.date
    won_list = won_list[won_list["day"].map(lambda d: isinstance(d, date) and d_start <= d <= d_end)]
    deal_lost = pd.DataFrame(
        [{"sale": key2sale(u), "source": r[0], "closed": r[1], "reason": r[2], "n": int(r[3])}
         for u, rows in (snap.get("dlost") or {}).items() if key2sale(u) for r in rows], columns=DEAL_LOST_COLS)
    deal_lost["closed"] = pd.to_datetime(deal_lost["closed"], errors="coerce").dt.date
    if "ldet" in snap:
        detail["created"] = pd.to_datetime(detail["created"], errors="coerce").dt.date
        m0 = datetime.now(TZ).date().replace(day=1)
        stages = lead_stage_summary(detail[detail["created"].map(lambda d: isinstance(d, date) and d >= m0)])
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
            "won_list": won_list}


LEAD_DETAIL_COLS = ["sale", "stage", "source", "created", "reason", "n"]
DEAL_LOST_COLS = ["sale", "source", "closed", "reason", "n"]
WON_LIST_COLS = ["sale", "id", "title", "amount", "source", "day", "link"]


def _norm_txt(t) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", str(t).lower().replace("đ", "d"))
    return " ".join(sorted(re.findall(r"[a-z0-9]+", "".join(c for c in t if unicodedata.category(c) != "Mn"))))


def is_excluded_source(src) -> bool:
    return _norm_txt(src) in {_norm_txt(x) for x in lead_exclude_sources()}


def lead_stage_summary(detail: pd.DataFrame) -> pd.DataFrame:
    """Số lead theo sale × stage, đã bỏ các nguồn trong lead_exclude_sources (dùng cho trang Tổng quan)."""
    keep = detail[~detail["source"].map(is_excluded_source)] if not detail.empty else detail
    return keep.groupby(["sale", "stage"], as_index=False)["n"].sum()[["sale", "stage", "n"]]

def _cfg_list(v) -> list[str]:
    return [str(x) for x in (v if isinstance(v, (list, tuple)) else [v]) if str(x).strip()]


HARA_SNAP = "sales-dashboard-haracrm.json"
HARA_VERSION = 3
HARA_SRC = r"""(async()=>{
const C=__CFG__;
const box=document.createElement('div');
box.style.cssText='position:fixed;z-index:2147483647;top:16px;right:16px;width:380px;padding:16px;background:white;color:rgb(34,34,34);border-radius:10px;box-shadow:0 4px 24px rgba(0,0,0,.25);font:14px/1.5 sans-serif;white-space:pre-line';
document.body.appendChild(box);
const say=t=>{box.innerText='📈 Cập nhật HaraCRM → Dashboard\n'+t;};
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const T=e=>String((e&&(e.innerText||e.textContent))||'').replace(/\s+/g,' ').trim();
const vis=e=>!!(e&&(e.offsetWidth||e.offsetHeight||e.getClientRects().length));
const norm=t=>String(t||'').toLowerCase().replace(/đ/g,'d').normalize('NFD').replace(/[̀-ͯ]/g,'').split(/[^a-z0-9.]+/).filter(Boolean).join(' ');
const names=(C.names||[]).map(norm);const team=norm(C.team);
const roots=[];const addRoot=r=>{if(!r||roots.includes(r))return;roots.push(r);for(const e of r.querySelectorAll('*')){if(e.shadowRoot)addRoot(e.shadowRoot);if(e.tagName==='IFRAME'){try{if(e.contentDocument)addRoot(e.contentDocument);}catch(er){}}}};
const qa=sel=>{roots.length=0;addRoot(document);return roots.flatMap(r=>[...r.querySelectorAll(sel)]);};
const CELL='[role=cell],[role=gridcell],[role=columnheader],[role=rowheader],td,th';
const hit=cells=>cells.some(c=>{const n=norm(c);return n===team||n.indexOf(team+' ')===0||names.includes(n)||names.some(x=>n.endsWith(' '+x));});
const titleOf=t=>{let e=t;for(let i=0;i<7;i++){e=e.parentElement;if(!e)break;const hs=[...e.querySelectorAll('h1,h2,h3,h4,h5,h6')].filter(h=>!t.contains(h)&&(h.compareDocumentPosition(t)&Node.DOCUMENT_POSITION_FOLLOWING));if(hs.length){const h=hs[hs.length-1];let sub='';const nx=h.nextElementSibling;if(nx&&!nx.contains(t)&&nx.tagName!=='TABLE'){sub=T(nx).slice(0,240);}return [T(h),sub];}}return ['Bảng',''];};
const scrape=tab=>{const out=[];for(const t of qa('table,[role=table],[role=grid],[role=treegrid]')){if(!vis(t)||(t.tagName!=='TABLE'&&t.querySelector('table')))continue;const isT=t.tagName==='TABLE';const trs=isT?[...t.querySelectorAll('tr')]:[...t.querySelectorAll('[role=row]')];if(!trs.length)continue;const kids=r=>isT?[...r.children]:[...r.querySelectorAll(CELL)];const hr=(isT&&t.querySelector('thead tr'))||trs.find(r=>r.querySelector('th,[role=columnheader]'))||trs[0];const headers=kids(hr).map(T);const rows=trs.filter(r=>r!==hr&&!(r.parentElement&&r.parentElement.tagName==='THEAD')).map(r=>kids(r).map(T)).filter(c=>c.length&&hit(c));if(!rows.length)continue;const [title,sub]=titleOf(t);out.push({tab:tab,title:title,sub:sub,headers:headers,rows:rows});}return out;};
const tabEl=name=>{const n=norm(name);return qa('button,a,[role=tab],li,span,div').filter(e=>vis(e)&&e.children.length<=2&&norm(T(e))===n).pop();};
try{
const getFilt=()=>{const dx=qa('span,div,p').filter(vis).map(T).filter(x=>x.length<300&&x.indexOf('Đang xem')>=0).map(x=>(x.match(/Đang xem:?\s*([0-9/]+\s*[–-]\s*[0-9/]+)/)||[])[1]).find(Boolean);return dx||qa('button,span,div,a').filter(vis).map(T).find(x=>x.length<60&&/^\d{1,2}\/\d{1,2}\s*[–-]\s*\d{1,2}\/\d{1,2}/.test(x))||'';};
const readAll=async()=>{const tables=[];const seen=new Set();const miss=[];for(const name of (C.tabs||[])){const el=tabEl(name);if(!el){miss.push(name);continue;}say('Đang mở tab '+name+'… (chờ bảng hiện ra)');el.click();let got=[];for(let i=0;i<Math.ceil((C.wait||15000)/500);i++){await sleep(500);got=scrape(name);if(got.length){await sleep(800);got=scrape(name);break;}}for(const x of got){const k=x.title+'|'+x.headers.join('|');if(!seen.has(k)){seen.add(k);tables.push(x);}}}if(!tables.length){for(const x of scrape('Trang hiện tại')){const k=x.title+'|'+x.headers.join('|');if(!seen.has(k)){seen.add(k);tables.push(x);}}}return {tables:tables,miss:miss};};
const curMonth=()=>{const m=(getFilt().match(/\d{1,2}\/(\d{1,2})\/\d{4}/)||[])[1];return m?+m:0;};
const periods=[];let miss=[];const nowD=new Date();const want=[];for(let k=(C.months||2)-1;k>=0;k--){want.push(new Date(nowD.getFullYear(),nowD.getMonth()-k,1));}
for(const d of want){const m=d.getMonth()+1;const label='Tháng '+m;const key=d.getFullYear()+'-'+String(m).padStart(2,'0');const btn=qa('button,a,span,div,li').filter(e=>vis(e)&&e.children.length<=1&&T(e)==='T'+m).pop();if(btn){say('Đang chọn '+label+' trên HaraCRM…');btn.click();let ok=false;for(let i=0;i<40;i++){await sleep(500);if(curMonth()===m){ok=true;break;}if(i===6){const ap=qa('button').filter(vis).find(e=>T(e)==='Áp dụng');if(ap)ap.click();}}if(!ok){periods.push({key:key,label:label,filter:getFilt(),tables:[],err:'Không chuyển được sang '+label});continue;}await sleep(1500);}else if(want.length>1&&curMonth()!==m){periods.push({key:key,label:label,filter:getFilt(),tables:[],err:'Không thấy nút T'+m});continue;}say('Đang đọc '+label+'…');const r=await readAll();miss=r.miss;periods.push({key:key,label:label,filter:getFilt(),tables:r.tables});}
const tables=periods.flatMap(p=>p.tables);const filt=periods.length?periods[periods.length-1].filter:getFilt();
if(!tables.length){qa('x');const nT=qa('table').length,nR=qa('[role=table],[role=grid]').length,nI=document.querySelectorAll('iframe').length,nS=roots.length-1,has=roots.some(r=>norm(T(r.body||r)).indexOf(team)>=0);say('❌ Không thấy bảng nào có dòng của '+C.team+' trên trang này.\nChẩn đoán: bảng HTML '+nT+' · role=table '+nR+' · iframe '+nI+' · vùng con '+nS+' · thấy chữ '+C.team+': '+(has?'có':'không')+'.\nChụp khung này gửi lại để chỉnh nút.');const x=document.createElement('button');x.innerText='Đóng';x.onclick=()=>box.remove();box.appendChild(x);return;}
const data={v:2,kind:'hara',bv:C.bv,at:Date.now(),host:location.host,filter:filt,team:C.team,missTabs:miss,periods:periods,tables:periods.length?periods[periods.length-1].tables:[]};
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


def hara_href(target_url: str, roster: pd.DataFrame) -> str:
    from urllib.parse import quote

    cfg = {"url": target_url, "team": str(secret("haracrm", "team", "S2.4")),
           "tabs": list(secret("haracrm", "tabs", ["Điều hành", "Năng lực NV"])),
           "names": list(roster["sale"]), "wait": int(secret("haracrm", "wait_ms", 15000)), "bv": HARA_VERSION,
           "months": int(secret("haracrm", "months", 2))}
    js = "".join(HARA_SRC.splitlines()).replace("__CFG__", json.dumps(cfg, ensure_ascii=False))
    return "javascript:" + quote(js, safe="()[]{}=;:,.'!*+/?&<>|-_~@$")


BOOKMARKLET_VERSION = 11  # tăng mỗi khi đổi nút → dashboard nhắc cài lại nút


def bookmarklet_href(target_url: str, roster: pd.DataFrame) -> str:
    from urllib.parse import quote

    stages, _ = lead_stage_config()
    cfg = {"url": target_url, "months": int(secret("bitrix24", "months", 3)), "stages": stages,
           "users": [[r.bitrix_id, r.sale, r.email] for r in roster.itertuples()],
           "excl": lead_exclude_sources(), "closed": lead_stage_config()[1],
           "reason": _cfg_list(secret("bitrix24", "unqual_reason_field", "")),
           "revDate": str(secret("bitrix24", "revenue_date_field", "Ngày ghi nhận doanh số") or "Ngày ghi nhận doanh số"),
           "lostReason": str(secret("bitrix24", "lost_reason_field", "") or ""), "bv": BOOKMARKLET_VERSION}
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
                             "title": ev.get("summary", "(không tiêu đề)"), "guests": ", ".join(g)})
        token = res.get("nextPageToken")
        if not token:
            break
    df = pd.DataFrame(rows, columns=["event_id", "sale", "email", "ts", "end", "title", "guests"])
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
                                     "title": ev.get("summary", "(không tiêu đề)"), "guests": ", ".join(g)})
                token = res.get("nextPageToken")
                if not token:
                    break
        except Exception as e:
            errors.append(f"{sale}: {e}")
    df = pd.DataFrame(rows, columns=["event_id", "sale", "email", "ts", "end", "title", "guests"])
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
                    "message_id": mid
                })
        except Exception as e:
            errors.append(f"{email}: {e}")
    df = pd.DataFrame(rows, columns=["sender", "ts", "subject", "rcpt", "est_amount", "message_id"])
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
            "recap_msg_id": found.message_id if found is not None else ""
        })
    return pd.DataFrame(out, columns=["event_id", "recap", "recap_subject", "recap_by", "est_amount", "recap_delay_str", "recap_msg_id"])


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
                             "reason": (["A5. Invalid Phone", "A6. Không đăng ký", "A2. Sinh viên Test",
                                         "F4. Khách trả lời không có nhu cầu", "B8.2. Không nghe máy"][int(r.integers(0, 5))]
                                        if st_ == "Unqualified" else ""), "n": 1})
    df = pd.DataFrame(rows, columns=LEAD_DETAIL_COLS)
    df = df[df["created"] >= _prev_month_start(today)]
    return df.groupby(["sale", "stage", "source", "created", "reason"], as_index=False)["n"].sum()


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
    out["won_list"] = pd.DataFrame({"sale": dw["sale"].values, "id": [str(10000 + i) for i in range(len(dw))],
                                  "title": [f"Haravan x {c} – Website" for c in rng.choice(DEMO_CLIENTS, len(dw))],
                                  "amount": dw["amount"].values,
                                  "source": rng.choice(["Website", "Facebook Ads", "Hotline", "Sale tự tìm"], len(dw)),
                                  "day": dw["ts"].dt.date.values, "link": ""}, columns=WON_LIST_COLS)
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
    "deals_new": pd.DataFrame(columns=["ts", "sale"]),
    "deals_won": pd.DataFrame(columns=["ts", "sale", "amount"]),
    "revenue": pd.DataFrame(columns=["ts", "sale", "amount"]),
    "meetings": pd.DataFrame(columns=["event_id", "sale", "email", "ts", "end", "title", "guests", "recap",
                                      "recap_subject", "recap_by", "est_amount", "recap_delay_str", "recap_msg_id"]),
    "calls": pd.DataFrame(columns=["ts", "sale", "duration"]),
}
raw_emails_df = pd.DataFrame(columns=["sender", "ts", "subject", "rcpt", "est_amount", "message_id"])


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
                         else pd.DataFrame(columns=["sender", "ts", "subject", "rcpt", "est_amount", "message_id"]))
        if raw_emails_df is not None and not mt.empty:
            rc = match_recaps(mt, raw_emails_df)
            data["meetings"] = mt.drop(columns=["recap", "est_amount", "recap_delay_str", "recap_msg_id"], errors="ignore").merge(rc, on="event_id", how="left")
            status.setdefault("Gmail (recap)", "✅ Đã kết nối")

    if configured("voicecloud") and not roster.empty:
        calls = attempt("VoiceCloud", load_calls, d_start, d_end)
        if calls is not None:
            data["calls"] = calls.assign(sale=calls["vc_id"].map(vc2sale)).dropna(subset=["sale"])

    for s in SOURCES:
        status.setdefault(s, "⚪ Chưa cấu hình")

with sale_slot:
    picked = st.multiselect("👤 Lọc theo Sale", roster["sale"].tolist(), placeholder="Tất cả Sale",
                            help="Chọn một hoặc vài Sale. Để trống = cả team.")
with st.sidebar:
    st.divider()
    st.subheader("Trạng thái kết nối")
    for k in SOURCES:
        st.markdown(f"{status[k]} &nbsp; {k}")
    
    st.caption(f"Dữ liệu cache {CACHE_TTL // 60} phút · Cập nhật: {now:%H:%M %d/%m}")

if roster.empty:
    st.title("📊 Dashboard Hiệu suất Sale")
    st.error("Chưa có danh sách Sale. Khai báo các mục `[[sales]]` trong Secrets.")
    for p in problems:
        st.markdown(f"- {p}")
    st.stop()

if data["revenue"].empty and not globals().get("snap"):   # webhook / demo: doanh thu theo deal won
    data["revenue"] = data["deals_won"][["ts", "sale", "amount"]]
sales_in_view = picked or roster["sale"].tolist()
for k in data:
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
closed_l = [c.lower() for c in closed_stages]
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
    row1, row2 = st.columns(4), st.columns(4)
    row1[0].metric("💰 Doanh thu", fmt_vnd(lb["revenue"].sum()),
                   help=(f"Tổng giá trị deal Won có **{(globals().get('snap') or {}).get('revField', [0, 'ngày ghi nhận doanh số'])[1]}** "
                         f"trong kỳ: {lb['revenue'].sum():,.0f} ₫".replace(",", ".")
                         if (globals().get("snap") or {}).get("revField")
                         else f"Tổng giá trị deal Won có ngày đóng trong kỳ: {lb['revenue'].sum():,.0f} ₫".replace(",", ".")))
    _rf = (globals().get("snap") or {}).get("revField")
    row1[1].metric("🏆 Deal won", fmt_int(lb["deals_won"].sum()),
                   help=f"Deal Won có **{_rf[1]}** trong kỳ" if _rf else "Deal chuyển sang trạng thái Won trong kỳ")
    row1[2].metric("🤝 Deal mới", fmt_int(lb["deals_new"].sum()), help="Deal tạo mới trong kỳ")
    row1[3].metric("📥 Lead mới", fmt_int(lb["leads_new"].sum()), help="Lead tạo mới trong kỳ")
    row2[0].metric("📊 Doanh thu Est (Meeting)", fmt_vnd(lb["est_revenue"].sum()),
                   help="Tổng giá trị dự kiến trích xuất từ báo giá đính kèm/trong email recap.")
    row2[1].metric("📞 Cuộc gọi", fmt_int(lb["calls"].sum()), help=f"Tổng thời lượng: {fmt_duration(lb['talk'].sum())}")
    row2[2].metric("📅 Meeting", fmt_int(lb["meetings"].sum()), help="Meeting có khách ngoài công ty")
    row2[3].metric("✉️ Tỷ lệ recap", f"{rate:.0%}" if tot_held else "—",
                    help=f"Đã recap / meeting đã diễn ra. Thiếu recap: {fmt_int(lb['recap_miss'].sum())}")

    st.divider()
    st.subheader("🏆 Hiệu suất theo Sale")
    n_months = (d_end.year * 12 + d_end.month) - (d_start.year * 12 + d_start.month) + 1
    kpi = lb["sale"].map(kpi_month).astype(float) * n_months
    view = pd.DataFrame({
        "Sale": lb["sale"],
        "Doanh thu (₫)": lb["revenue"],
        "KPI (₫)": kpi,
        "% KPI": (lb["revenue"] / kpi.replace(0, np.nan) * 100).round(1),
        "Est Doanh thu": lb["est_revenue"],
        "Lead mới": lb["leads_new"], "Lead tồn": lb["lead_open"],
        "Deal mới": lb["deals_new"], "Deal won": lb["deals_won"],
        "Cuộc gọi": lb["calls"], "Thời lượng gọi": lb["talk"].map(fmt_duration),
        "Meeting": lb["meetings"], "Đã recap": lb["recap_done"], "Thiếu recap": lb["recap_miss"],
        "Tỷ lệ recap": lb["recap_rate"].map(lambda v: "—" if pd.isna(v) else f"{v:.0%}"),
    })
    view["KPI (₫)"] = kpi.map(lambda v: "" if pd.isna(v) else f"{v:,.0f}")
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "Sale": st.column_config.TextColumn(pinned=True),
        "Doanh thu (₫)": st.column_config.NumberColumn(format="localized"),
        "KPI (₫)": st.column_config.TextColumn(
                                                 help=f"KPI doanh thu/tháng × {n_months} tháng trong kỳ" if n_months > 1
                                                 else "KPI doanh thu của tháng"),
        "% KPI": st.column_config.ProgressColumn("% KPI", format="%.0f%%", min_value=0, max_value=100,
                                                 help="Doanh thu ÷ KPI (loading KPI)"),
        "Est Doanh thu": st.column_config.NumberColumn(format="localized"),
        "Thời lượng gọi": st.column_config.TextColumn(),
        "Thiếu recap": st.column_config.NumberColumn(),
        "Tỷ lệ recap": st.column_config.TextColumn(),
    })
    st.download_button("⬇️ Tải bảng (CSV)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"hieu_suat_sale_{d_start:%Y%m%d}_{d_end:%Y%m%d}.csv", mime="text/csv")
    if not DEMO:
        _render_hara()


def _render_hara():
    st.divider()
    team = secret("haracrm", "team", "S2.4")
    st.subheader(f"📈 Chỉ số funnel HaraCRM — team {team}")
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
    st.caption(f"Số đã tính sẵn trên HaraCRM · kỳ: **{per.get('filter') or pick}** · cập nhật {at_:%H:%M %d/%m}.")
    if per.get("err"):
        st.warning(f"Lần cập nhật này không lấy được {pick}: {per['err']}.")
    h = {"tables": per.get("tables", [])}
    want = [_plain(x) for x in secret("haracrm", "tables", ["Xếp hạng funnel", "NV trong phạm vi lọc"])]
    shown = [t for t in h.get("tables", []) if not want or any(w in _plain(t.get("title", "")) for w in want)]
    shown.sort(key=lambda t: next((i for i, w in enumerate(want) if w in _plain(t.get("title", ""))), 99))
    if not shown:
        st.warning("Dữ liệu HaraCRM không có bảng nào đúng tên cần lấy "
                   f"({', '.join(secret('haracrm', 'tables', ['Xếp hạng funnel', 'NV trong phạm vi lọc']))}).")
    for t in shown:
        headers = list(t.get("headers") or [])
        rows = [list(r) for r in t.get("rows") or []]
        width = max([len(headers)] + [len(r) for r in rows])
        headers += [""] * (width - len(headers))
        cols, seen = [], {}
        for c in headers:   # tên cột trùng/rỗng → thêm số để không lỗi
            c = c or "—"
            seen[c] = seen.get(c, 0) + 1
            cols.append(c if seen[c] == 1 else f"{c} ({seen[c]})")
        df = pd.DataFrame([r + [""] * (width - len(r)) for r in rows], columns=cols)
        st.markdown(f"**{t.get('title') or 'Bảng'}** · _{t.get('tab', '')}_")
        if t.get("sub"):
            st.caption(t["sub"])
        hl = [c for c in df.columns if _plain(c).replace(" ", "") in ("lead→win", "lead->win")]
        sty = (df.style.hide(axis="index")
               .set_table_styles([{"selector": "th", "props": [("white-space", "normal"), ("font-weight", "700"),
                                                                ("background-color", "rgba(62, 180, 137, 0.18)")]}])
               .map_index(lambda _: "font-weight: 700; background-color: rgba(62, 180, 137, 0.18);", axis="columns"))
        if hl:
            sty = sty.set_properties(subset=hl, **{"background-color": "rgba(255, 193, 7, 0.22)", "font-weight": "bold"})
        st.table(sty)


def page_leads():
    st.caption(f"Số lead **tạo trong tháng này và tháng trước** hiện đang nằm ở từng stage trên Bitrix24. "
               f"Cột **Tồn** = tổng các stage trừ {', '.join(closed_stages) or '—'}.")
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
        f = f[f["created"].map(lambda d: isinstance(d, date) and t0_ <= d <= t1_)]
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
        lost = lost[lost["closed"].map(lambda d: isinstance(d, date) and t0_ <= d <= t1_)]
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


def _show_reason_table(pv: pd.DataFrame, total_col: str, help_: str, fname: str, label: str):
    num = [c for c in pv.columns if c != "Sale"]
    disp = pv.assign(Sale=pv["Sale"].astype(str).str.replace(" ", "\u00a0"))  # giữ tên Sale trên 1 dòng
    styled = (disp.style
              .hide(axis="index")
              .set_properties(subset=[total_col], **{"background-color": "rgba(255, 193, 7, 0.22)", "font-weight": "bold"})
              .set_properties(subset=num, **{"text-align": "right"})
              .set_table_styles([{"selector": "th", "props": [("white-space", "normal"), ("word-break", "break-word"),
                                                               ("vertical-align", "bottom"), ("min-width", "70px")]},
                                 {"selector": "td", "props": [("white-space", "normal")]},
                                 {"selector": "td.col0", "props": [("white-space", "nowrap")]}])
              .apply(lambda r: ["font-weight: bold" if r["Sale"] == "TỔNG" else "" for _ in r], axis=1)
              .format(lambda v: f"{v:,}" if v else "", subset=num))
    st.table(styled)  # bảng thường: tiêu đề dài tự xuống hàng thay vì bị cắt
    st.download_button(label, pv.to_csv(index=False).encode("utf-8-sig"), file_name=fname, mime="text/csv")


def _old_button_msg(snap_: dict):
    at_ = datetime.fromtimestamp(snap_.get("at", 0) / 1000, TZ)
    st.warning(f"Dữ liệu Bitrix hiện có (gửi lúc **{at_:%H:%M %d/%m}**) đến từ **nút Cập nhật Bitrix cũ** nên chưa có phần này. "
               "Leader làm lại: mở trang **📥 Cập nhật Bitrix** → xóa nút cũ trên thanh dấu trang → kéo nút mới vào → "
               "mở Bitrix24, bấm nút mới → **📤 Gửi sang dashboard**.")


def _show_reason_list(u: pd.DataFrame, fname: str, label: str):
    """Lý do nhập tay: mỗi lý do 1 hàng, nhóm theo Sale (thay vì mỗi lý do 1 cột)."""
    u = u.copy()
    u["reason"] = u["reason"].map(lambda v: " ".join(str(v).split())).replace("", "(Chưa ghi lý do)")
    g = u.groupby(["sale", "reason"], as_index=False)["n"].sum()
    rank = {s_: i for i, s_ in enumerate(lb["sale"])}
    g["_s"] = g["sale"].map(rank).fillna(len(rank))
    g["_blank"] = g["reason"].eq("(Chưa ghi lý do)")
    g = g.sort_values(["_s", "_blank", "n", "reason"], ascending=[True, True, False, True])
    out = g.rename(columns={"sale": "Sale", "reason": "Lý do chi tiết", "n": "Số lead"})[["Sale", "Lý do chi tiết", "Số lead"]]
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
              if snap_ is not None else [["", "Lý do Unqualified"]])
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
    for idx, (code, label) in order:
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
        if "chi tiet" in _plain(label) or "DETAIL" in str(code).upper():   # lý do sale tự nhập tay → liệt kê theo hàng
            _show_reason_list(u, f"unqualified_{safe}_{now:%Y%m%d}.csv", f"⬇️ Tải bảng {label} (CSV)")
        else:
            _show_reason_table(_reason_table(u, "Unqual"), "Unqual", "Tổng lead Unqualified",
                               f"unqualified_{safe}_{now:%Y%m%d}.csv", f"⬇️ Tải bảng {label} (CSV)")


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
            **{c: st.column_config.NumberColumn(help="Không tính vào Tồn") for c in closed_stages if c in stages},
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
                m = m.drop(columns=["recap", "est_amount", "recap_delay_str", "recap_msg_id"],
                           errors="ignore").merge(rc, on="event_id", how="left")
    m = m[m["sale"].isin(sales_in_view)].copy()
    m["status"] = m.apply(recap_status, axis=1) if not m.empty else pd.Series(dtype=str)
    return m


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

    st.markdown("**📋 Chi tiết từng meeting**")
    ALL = "Tất cả"
    one = st.pills("Lọc trạng thái", [ALL] + order, selection_mode="single", default=ALL,
                   label_visibility="collapsed", key="mt_status_pill")
    pick = order if (not one or one == ALL) else [one]
    detail = mm[mm["status"].isin(pick)].sort_values("ts", ascending=False)
    st.caption(f"{len(detail)} meeting")
    st.dataframe(
        pd.DataFrame({
            "Thời gian": detail["ts"].dt.tz_localize(None), 
            "Sale": detail["sale"], 
            "Meeting": detail["title"],
            "Khách": detail["guests"], 
            "Trạng thái recap": detail["status"],
            "Email recap": detail.get("recap_subject", pd.Series("", index=detail.index)).fillna(""),
            "Thời gian gửi recap sau meeting": detail.get("recap_delay_str", pd.Series("—", index=detail.index)),
            "Est Doanh thu (₫)": detail.get("est_amount", pd.Series(0, index=detail.index)),
            "Khớp theo": detail.get("recap_by", pd.Series("", index=detail.index)).fillna(""),
        }),
        hide_index=True, width="stretch",
        column_config={
            "Thời gian": st.column_config.DatetimeColumn(format="DD/MM HH:mm"),
            "Est Doanh thu (₫)": st.column_config.NumberColumn(format="localized"),
        },
    )


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
        msg_id = getattr(em, "message_id", "")
        link = f"https://mail.google.com/mail/#inbox/{msg_id}" if msg_id else "https://mail.google.com/"
        rows.append({
            "Ngày gửi recap": em.ts.tz_localize(None) if not pd.isna(em.ts) else None,
            "Sale": sale_name,
            "Tiêu đề mail recap": em.subject,
            "Estimate doanh thu": em.est_amount,
            "Mở recap": link
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
        "Mở recap": st.column_config.LinkColumn("Mở recap", display_text="🔗 Xem chi tiết mail")
    })


def page_trends():
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📞 Số cuộc gọi**")
        st.plotly_chart(trend_chart(trend(data["calls"]), COLOR_CALLS, "Cuộc gọi"), width="stretch")
    with c2:
        st.markdown("**📅 Số meeting**")
        st.plotly_chart(trend_chart(trend(mt), COLOR_MEETINGS, "Meeting"), width="stretch")


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
    k = st.columns(3)
    k[0].metric("🏆 Số deal won", fmt_int(len(w)))
    k[1].metric("💰 Tổng tiền", fmt_vnd(w["amount"].sum()))
    k[2].metric("📊 Trung bình / deal", fmt_vnd(w["amount"].mean() if len(w) else 0))
    st.caption(f"Deal Won có **{rf[1] if rf else 'ngày Won'}** trong kỳ {d_start:%d/%m/%Y} – {d_end:%d/%m/%Y}. "
               "Bấm **Mở deal ↗** để xem deal trên Bitrix24.")
    if w.empty:
        st.info("Không có deal won nào với bộ lọc hiện tại.")
        return
    view = (w.sort_values(["day", "amount"], ascending=[False, False])
            .rename(columns={"day": "Ngày ghi nhận", "sale": "Sale", "title": "Tên deal", "amount": "Số tiền (₫)",
                             "source": "Source", "link": "Link deal"})
            [["Ngày ghi nhận", "Sale", "Tên deal", "Số tiền (₫)", "Source", "Link deal"]])
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "Ngày ghi nhận": st.column_config.DateColumn(format="DD/MM/YYYY"),
        "Tên deal": st.column_config.TextColumn(width="large"),
        "Số tiền (₫)": st.column_config.NumberColumn(format="localized"),
        "Link deal": st.column_config.LinkColumn(display_text="Mở deal ↗"),
    })
    st.download_button("⬇️ Tải danh sách deal won (CSV)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"deal_won_{d_start:%Y%m%d}_{d_end:%Y%m%d}.csv", mime="text/csv")


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
        st.page_link(pages["Báo cáo"][0], label="→ Xem Tổng quan", icon="📊")
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
        st.page_link(pages["Báo cáo"][0], label="→ Xem Tổng quan", icon="📊")

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


pages = {"Báo cáo": [
    st.Page(page_overview, title="Tổng quan", icon="📊", default=True),
    st.Page(page_leads, title="Lead tồn theo stage", icon="🗂️", url_path="lead-ton"),
    st.Page(page_meetings, title="Meeting & recap", icon="✉️", url_path="meeting-recap"),
    st.Page(page_all_recap_emails, title="Danh sách email recap", icon="📑", url_path="danh-sach-recap"),
    st.Page(page_trends, title="Xu hướng hoạt động", icon="📈", url_path="xu-huong"),
    st.Page(page_won_deals, title="Danh sách deal won", icon="💰", url_path="deal-won"),
]}
settings = []
if configured("google_oauth"):
    settings.append(st.Page(page_google_connect, title="Kết nối Google", icon="🔑", url_path="ket-noi-google"))
if not DEMO and bitrix_mode() == "bookmarklet":
    settings.append(st.Page(page_bitrix_update, title="Cập nhật Bitrix", icon="📥", url_path="cap-nhat-bitrix"))
if not DEMO:
    settings.append(st.Page(page_hara_update, title="Cập nhật HaraCRM", icon="📈", url_path="cap-nhat-haracrm"))
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
