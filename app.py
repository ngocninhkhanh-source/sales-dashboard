"""
Dashboard Hiệu suất Sale
========================
Nguồn dữ liệu:
  • Bitrix24        : Lead mới, Lead tồn theo stage, Deal mới, Deal thắng, Doanh thu (tổng giá trị deal thắng)
  • VoiceCloud      : số cuộc gọi & thời lượng gọi
  • Google Calendar : số Meeting với khách
  • Gmail           : kiểm tra email recap (không giới hạn 48h) + Estimate doanh thu từ báo giá

Google có 2 cách kết nối:
  A. CHẾ ĐỘ LEADER (khuyên dùng, không cần IT): Sale CC leader vào lời mời meeting và email recap
     → app chỉ đọc lịch + hộp thư của leader. Leader đăng nhập Google 1 lần ở trang "🔑 Kết nối Google".
  B. Service account + Domain-wide delegation (cần Super Admin Google Workspace).
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

st.set_page_config(page_title="Dashboard Hiệu suất Sale", page_icon="📊", layout="wide")


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
    """Định dạng thời gian chênh lệch từ meeting_end tới recap_time dạng tiếng Việt."""
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
                        client_secret=c["client_secret"], token_uri=TOKEN_URI, scopes=OAUTH_SCOPES)
    return build(api, version, credentials=creds, cache_discovery=False)


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
    # Lấy email đến thời điểm hiện tại (bỏ giới hạn t1 + 48h cũ)
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
                rows.append({"sender": email.lower(), "ts": int(m.get("internalDate", 0)) / 1000,
                             "subject": subject_text, "rcpt": ",".join(rcpt), "est_amount": est_amount})
        except Exception as e:
            errors.append(f"{email}: {e}")
    df = pd.DataFrame(rows, columns=["sender", "ts", "subject", "rcpt", "est_amount"])
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
    """Gán email recap cho từng meeting. Miễn là gửi TỪ LÚC MEETING BẮT ĐẦU cho tới HIỆN TẠI."""
    g = dict(secret("gmail") or {})
    ways = list(g.get("match_by", ["guest_email", "title_name"]))
    out = []
    now_dt = datetime.now(TZ)
    for r in meetings.itertuples():
        found, how, amount, time_diff_str = None, "", 0.0, "—"
        if r.end <= now_dt and not emails.empty:
            # Lấy các email gửi từ khi bắt đầu meeting trở về sau (bỏ giới hạn window 48h)
            c = emails[(emails["sender"] == str(r.email).lower()) & (emails["ts"] >= r.ts)]
            guests = {x.strip() for x in str(r.guests).split(",") if x.strip()}
            name = title_name_tokens(r.title)
            for e in c.sort_values("ts").itertuples():
                if ("guest_email" in ways and guests & set(e.rcpt.split(","))) or \
                   ("title_name" in ways and name and name <= set(tokens(e.subject))):
                    found, how, amount = e, ("email khách" if "guest_email" in ways else "tên tiêu đề"), e.est_amount
                    time_diff = e.ts - r.end
                    time_diff_str = fmt_time_delta(time_diff)
                    break
        out.append({
            "event_id": r.event_id, 
            "recap": found is not None,
            "recap_subject": found.subject if found is not None else "", 
            "recap_by": how, 
            "est_amount": amount,
            "recap_delay_str": time_diff_str
        })
    return pd.DataFrame(out, columns=["event_id", "recap", "recap_subject", "recap_by", "est_amount", "recap_delay_str"])


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
    
    # Sinh dữ liệu thời gian recap trễ giả lập
    delays = [fmt_time_delta(timedelta(minutes=int(m))) for m in rng.integers(15, 2800, len(meetings))]
    meetings["recap_delay_str"] = np.where(meetings["recap"], delays, "—")

    calls = _demo_events(rng, w, days, 22)
    calls["duration"] = rng.gamma(2.0, 70, len(calls)).round()
    return {
        "leads_new": _demo_events(rng, w, days, 2.2),
        "lead_stages": pd.DataFrame(
            [{"sale": s_, "stage": st_, "n": int(n_)} for s_ in DEMO_SALES
             for st_, n_ in zip(LEAD_STAGES_DEFAULT, np.random.default_rng(sum(map(ord, s_))).poisson([9, 7, 5, 3, 4, 12]))]),
        "deals_new": _demo_events(rng, w, days, 0.9),
        "deals_won": (dw := _demo_events(rng, w, days, 0.3)).assign(amount=rng.lognormal(17.6, 0.6, len(dw)).round(-5)),
        "meetings": meetings,
        "calls": calls,
    }


# ════════════════════════════════════════════════════════════════════════════
# SIDEBAR — BỘ LỌC
# ════════════════════════════════════════════════════════════════════════════
SOURCES = ["Danh sách Sale", "Bitrix24", "Google Calendar", "Gmail (recap)", "VoiceCloud"]
DEMO = not any(configured(s) for s in ("sales", "google_sheets", "bitrix24", "voicecloud", "google_calendar",
                                       "google_oauth"))

with st.sidebar:
    st.header("⚙️ Bộ lọc")
    mode_options = ["Hôm nay", "Hôm qua", "Tuần này", "Tháng này", "Quý này", "Tùy chọn khoảng ngày"]
    mode = st.selectbox("Mốc thời gian", mode_options, index=2)
    
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
    st.caption(f"Kỳ báo cáo: **{d_start:%d/%m/%Y} → {d_end:%d/%m/%Y}**")
    sale_slot = st.container()
    if st.button("🔄 Làm mới dữ liệu", width="stretch"):
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
    "deals_new": pd.DataFrame(columns=["ts", "sale"]),
    "deals_won": pd.DataFrame(columns=["ts", "sale", "amount"]),
    "meetings": pd.DataFrame(columns=["event_id", "sale", "email", "ts", "end", "title", "guests", "recap",
                                      "recap_subject", "recap_by", "est_amount", "recap_delay_str"]),
    "calls": pd.DataFrame(columns=["ts", "sale", "duration"]),
}


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

    if configured("bitrix24") and not roster.empty:
        bx = attempt("Bitrix24", load_bitrix, tuple(roster["bitrix_id"]), d_start, d_end)
        if bx is not None:
            for k in ("leads_new", "deals_new", "deals_won", "lead_stages"):
                data[k] = bx[k].assign(sale=bx[k]["bitrix_id"].map(id2sale)).dropna(subset=["sale"])

    g_leader = leader_mode()
    g_sa = configured("google_calendar") and configured("gcp_service_account")
    if (g_leader or g_sa) and not roster.empty:
        m = attempt("Google Calendar", load_meetings_leader if g_leader else load_meetings,
                    tuple(roster["email"]), tuple(roster["sale"]), d_start, d_end)
        if m is not None:
            data["meetings"] = m.assign(recap=pd.NA, est_amount=0.0, recap_delay_str="—")

    gmail_on = g_leader or (configured("gmail") and configured("gcp_service_account"))
    mt = data["meetings"]
    if gmail_on and not mt.empty:
        senders = tuple(sorted(set(mt.loc[mt["end"] <= now, "email"])))
        em = (attempt("Gmail (recap)", load_recap_emails, senders, d_start, d_end, g_leader) if senders
              else pd.DataFrame(columns=["sender", "ts", "subject", "rcpt", "est_amount"]))
        if em is not None:
            rc = match_recaps(mt, em)
            data["meetings"] = mt.drop(columns=["recap", "est_amount", "recap_delay_str"], errors="ignore").merge(rc, on="event_id", how="left")
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
    st.error("Chưa có danh sách Sale. Khai báo các mục `[[sales]]` trong Secrets (xem hướng dẫn).")
    for p in problems:
        st.markdown(f"- {p}")
    st.stop()

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
    .merge(data["deals_won"].groupby("sale")["amount"].sum().rename("revenue"), on="sale", how="left")
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


def page_overview():
    tot_held = int(held.sum())
    rate = lb["recap_done"].sum() / tot_held if tot_held else 0
    row1, row2 = st.columns(4), st.columns(4)
    row1[0].metric("💰 Doanh thu", fmt_vnd(lb["revenue"].sum()),
                   help=f"Tổng giá trị deal Won có ngày đóng trong kỳ (Bitrix24): {lb['revenue'].sum():,.0f} ₫".replace(",", "."))
    row1[1].metric("🏆 Deal thắng", fmt_int(lb["deals_won"].sum()), help="Deal chuyển sang trạng thái thắng trong kỳ, theo ngày đóng")
    row1[2].metric("🤝 Deal mới", fmt_int(lb["deals_new"].sum()), help="Deal tạo mới trong kỳ (Bitrix24)")
    row1[3].metric("📥 Lead mới", fmt_int(lb["leads_new"].sum()), help="Lead tạo mới trong kỳ (Bitrix24)")
    row2[0].metric("📊 Doanh thu Est (Meeting)", fmt_vnd(lb["est_revenue"].sum()),
                   help="Tổng giá trị dự kiến trích xuất từ báo giá đính kèm/trong email recap của các meeting.")
    row2[1].metric("📞 Cuộc gọi", fmt_int(lb["calls"].sum()), help=f"Tổng thời lượng: {fmt_duration(lb['talk'].sum())}")
    row2[2].metric("📅 Meeting", fmt_int(lb["meetings"].sum()), help="Meeting có khách ngoài công ty (Google Calendar)")
    row2[3].metric("✉️ Tỷ lệ recap", f"{rate:.0%}" if tot_held else "—",
                    help=f"Đã recap / meeting đã diễn ra. Thiếu recap: {fmt_int(lb['recap_miss'].sum())}")

    st.divider()
    st.subheader("🏆 Hiệu suất theo Sale")
    view = pd.DataFrame({
        "Tên Sale": lb["sale"],
        "Doanh thu (₫)": lb["revenue"],
        "Est Doanh thu": lb["est_revenue"],
        "Lead mới": lb["leads_new"], "Lead tồn": lb["lead_open"],
        "Deal mới": lb["deals_new"], "Deal thắng": lb["deals_won"],
        "Cuộc gọi": lb["calls"], "Thời lượng gọi": lb["talk"].map(fmt_duration),
        "Meeting": lb["meetings"], "Đã recap": lb["recap_done"], "Thiếu recap": lb["recap_miss"],
        "Tỷ lệ recap": lb["recap_rate"].map(lambda v: "—" if pd.isna(v) else f"{v:.0%}"),
    })
    st.dataframe(view, hide_index=True, width="stretch", column_config={
        "Tên Sale": st.column_config.TextColumn(pinned=True),
        "Doanh thu (₫)": st.column_config.NumberColumn(format="localized", help="Tổng giá trị deal Won, ngày đóng trong kỳ"),
        "Est Doanh thu": st.column_config.NumberColumn(format="localized", help="Tổng giá trị dự kiến từ báo giá trong email recap"),
        "Thời lượng gọi": st.column_config.TextColumn(help="Tổng thời lượng, giờ:phút:giây"),
        "Thiếu recap": st.column_config.NumberColumn(help="Meeting đã diễn ra mà chưa gửi email recap"),
        "Tỷ lệ recap": st.column_config.TextColumn(help="Đã recap / meeting đã diễn ra."),
    })
    st.download_button("⬇️ Tải bảng (CSV)", view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"hieu_suat_sale_{d_start:%Y%m%d}_{d_end:%Y%m%d}.csv", mime="text/csv")


def page_leads():
    st.caption(f"Số lead hiện đang nằm ở từng stage trên Bitrix24, tính tại thời điểm xem (không phụ thuộc kỳ lọc). "
               f"Cột **Tồn** = tổng các stage trừ {', '.join(closed_stages) or '—'}.")
    stage_view = stage_tbl.loc[lb["sale"]].rename(columns={"lead_open": "Tồn"})
    stage_view["Tổng"] = stage_view[stages].sum(axis=1)
    stage_view = stage_view.sort_values("Tồn", ascending=False)
    if len(stage_view) > 1:
        stage_view = pd.concat([stage_view, stage_view.sum().to_frame("TỔNG").T])
    stage_view = stage_view.rename_axis("Tên Sale").rename_axis(None, axis=1).reset_index()
    st.dataframe(stage_view, hide_index=True, width="stretch", column_config={
        "Tên Sale": st.column_config.TextColumn(pinned=True),
        "Tồn": st.column_config.NumberColumn(help="Tổng lead chưa ra kết quả"),
        "Tổng": st.column_config.NumberColumn(help="Tổng tất cả stage trong bảng"),
        **{c: st.column_config.NumberColumn(help="Không tính vào Tồn") for c in closed_stages if c in stages},
    })
    st.download_button("⬇️ Tải bảng lead (CSV)", stage_view.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"lead_ton_theo_stage_{now:%Y%m%d}.csv", mime="text/csv")


def page_meetings():
    k = st.columns(5)
    k[0].metric("📅 Meeting", fmt_int(lb["meetings"].sum()))
    k[1].metric("✅ Đã recap", fmt_int(lb["recap_done"].sum()))
    k[2].metric("❌ Thiếu recap", fmt_int(lb["recap_miss"].sum()), help="Chưa có email recap tính tới hiện tại")
    tot = int(held.sum())
    k[3].metric("✉️ Tỷ lệ recap", f"{lb['recap_done'].sum() / tot:.0%}" if tot else "—")
    k[4].metric("💰 Tổng Est", fmt_vnd(mt["est_amount"].sum()), help="Tổng giá trị ước tính từ báo giá gửi kèm email recap")
    
    order = [ST_DONE, ST_MISS, ST_UPCOMING, ST_UNKNOWN]
    present = [s for s in order if st_counts.get(s, pd.Series(dtype=int)).sum() > 0]
    if mt.empty:
        st.caption("Không có meeting nào trong kỳ.")
    else:
        stack = (st_counts.reindex(index=lb.sort_values("meetings")["sale"], fill_value=0)[present]
                 .reset_index().melt(id_vars="sale", var_name="Trạng thái", value_name="Số meeting"))
        fig = px.bar(stack, x="Số meeting", y="sale", color="Trạng thái", orientation="h",
                     color_discrete_map=STATUS_COLORS, category_orders={"Trạng thái": present},
                     labels={"sale": ""})
        fig.update_traces(marker_line_width=2, marker_line_color="rgba(255,255,255,0.9)",
                          hovertemplate="<b>%{y}</b><br>%{fullData.name}: %{x}<extra></extra>")
        fig.update_layout(height=max(300, 40 * len(sales_in_view) + 90), margin=dict(l=0, r=10, t=10, b=0),
                          barmode="stack", bargap=0.35, legend=dict(orientation="h", y=1.08, x=0, title=None),
                          xaxis=dict(showgrid=True, dtick=max(1, int(np.ceil(lb["meetings"].max() / 8)))),
                          yaxis=dict(showgrid=False))
        st.plotly_chart(fig, width="stretch")

        st.markdown("**📋 Chi tiết từng meeting**")
        pick = st.pills("Lọc trạng thái", present, selection_mode="multi",
                        default=[s for s in (ST_MISS, ST_DONE) if s in present] or present, label_visibility="collapsed")
        detail = mt[mt["status"].isin(pick or present)].sort_values("ts", ascending=False)
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
                "Thời gian gửi recap sau meeting": st.column_config.TextColumn(help="Khoảng thời gian từ lúc kết thúc meeting đến khi gửi email recap"),
                "Est Doanh thu (₫)": st.column_config.NumberColumn(format="localized", help="Giá trị trích xuất từ báo giá gửi trong email recap"),
            },
        )
        if not gmail_on:
            st.caption("Chưa cấu hình Gmail nên chưa kiểm tra được recap.")


def page_trends():
    st.caption("Theo giờ khi xem 1 ngày, theo ngày khi xem nhiều ngày.")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**📞 Số cuộc gọi**")
        st.plotly_chart(trend_chart(trend(data["calls"]), COLOR_CALLS, "Cuộc gọi"), width="stretch")
    with c2:
        st.markdown("**📅 Số meeting**")
        st.plotly_chart(trend_chart(trend(mt), COLOR_MEETINGS, "Meeting"), width="stretch")


def page_google_connect():
    c = dict(secret("google_oauth") or {})
    st.markdown(
        "Trang dành cho **leader**: đăng nhập Google 1 lần để dashboard đọc lịch (meeting) và hộp thư "
        "(email recap mà Sale CC cho leader). Quyền chỉ đọc.")
    if leader_mode():
        st.success(f"Đang kết nối với hộp thư của leader {c.get('leader_email', '')}.")
    st.markdown("**Bước 1.** Bấm nút đăng nhập Google bằng tài khoản leader.")
    st.link_button("🔑 Đăng nhập Google (leader)", oauth_login_url(), type="primary")
    st.markdown("**Bước 2.** Copy toàn bộ địa chỉ trên thanh địa chỉ (bắt đầu bằng `http://localhost`) dán vào ô dưới:")
    pasted = st.text_input("Địa chỉ trang lỗi", placeholder="http://localhost/?code=4/0Ab...", label_visibility="collapsed")
    code = extract_code(pasted) if pasted else st.query_params.get("code", "")
    if not code:
        return
    cache = st.session_state.setdefault("oauth_results", {})
    if code not in cache:
        try:
            cache[code] = oauth_exchange(code)
        except Exception as e:
            cache[code] = {"error": type(e).__name__, "error_description": str(e)}
    tok = cache[code]
    if "refresh_token" not in tok:
        st.error(f"Không lấy được mã kết nối: {tok.get('error_description') or tok.get('error') or tok}.")
        return
    st.markdown(f"**Bước 3.** Đã kết nối tài khoản **{tok.get('email')}**. Copy dòng dưới dán vào Secrets:")
    st.code(f'refresh_token = "{tok["refresh_token"]}"', language="toml")


pages = {"Báo cáo": [
    st.Page(page_overview, title="Tổng quan", icon="📊", default=True),
    st.Page(page_leads, title="Lead tồn theo stage", icon="🗂️", url_path="lead-ton"),
    st.Page(page_meetings, title="Meeting & recap", icon="✉️", url_path="meeting-recap"),
    st.Page(page_trends, title="Xu hướng hoạt động", icon="📈", url_path="xu-huong"),
]}
if configured("google_oauth"):
    pages["Cài đặt"] = [st.Page(page_google_connect, title="Kết nối Google", icon="🔑", url_path="ket-noi-google")]
pg = st.navigation(pages, position="sidebar", expanded=True)

st.title(f"{pg.icon} {pg.title}")
who = ", ".join(picked) if picked and len(picked) <= 3 else f"{len(sales_in_view)} Sale"
st.caption(f"Kỳ: **{mode}** · {d_start:%d/%m/%Y} – {d_end:%d/%m/%Y} · **{who}**")
if DEMO:
    st.info("🧪 **Chế độ DEMO** — đang hiển thị dữ liệu giả định cùng thời gian trễ recap mẫu.", icon="ℹ️")
if problems:
    with st.expander(f"⚠️ {len(problems)} vấn đề khi tải dữ liệu — bấm để xem"):
        for p in problems:
            st.markdown(f"- {p}")

pg.run()

st.divider()
st.caption("Nguồn: Bitrix24 (Lead, Deal, Doanh thu) · VoiceCloud · Google Calendar · Gmail (Báo giá/Estimate & Recap delay)")
st.caption("Dashboard created by Khanh Ngoc Ninh")
