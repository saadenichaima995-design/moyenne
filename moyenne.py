# moyenne.py — Portail Mega Formation (Sheets only + Planning structuré + Liens Drive manuels)
# ✅ Sidebar = Connexion Employé (à gauche)
# ✅ Centre = Espace Stagiaire
# ✅ Planning = الموظف يكتب (jour + وقت + matière + prof + couleur) والمتكون يشوف tableau ملون
# ✅ Paiements = حسب السنوات (2025/2026...) + أشهر Jan..Dec
# ✅ Supports de cours = روابط Google Drive (manual paste) + المتكون يلقى الدروس ويحملها
# ✅ Import Excel stagiaires (full_name + phone)
# ✅ CRUD: الموظف ينجم يزيد/يعدّل/يفسخ (Planning + Supports + Stagiaires + Notes + Paiements)
# ✅ Notes: الموظف ينجم يعدّل/يفسخ النوط اللي زادهم + جدول "Mes notes" حسب الاختصاص
# ✅ بدون Drive API (تفادياً لمشاكل Service Account quota/permissions)
# ✅ بدون st.link_button (باش ما يطيحش حسب نسخة Streamlit)

import uuid
import base64
import io
import re
from datetime import datetime

import streamlit.components.v1 as components
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError
from PIL import Image

# =========================================================
# CONFIG
# =========================================================
st.set_page_config(page_title="Portail Mega Formation", page_icon="🧩", layout="wide")

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DAYS_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAY_ORDER = {d: i for i, d in enumerate(DAYS_FR)}

REQUIRED_SHEETS = {
    "Branches": ["branch", "staff_password", "is_active", "created_at"],

    "Programs": ["program_id", "branch", "program_name", "is_active", "created_at"],
    "Groups": ["group_id", "branch", "program_name", "group_name", "is_active", "created_at"],

    "Trainees": ["trainee_id", "full_name", "phone", "branch", "program", "group", "status", "created_at"],

    # student_name = الاسم اللي كتبو المتكون في التسجيل
    "Accounts": ["phone", "password", "trainee_id", "student_name", "created_at", "last_login"],

    "Subjects": ["subject_id", "branch", "program", "group", "subject_name", "is_active", "created_at"],

    "Grades": ["grade_id", "trainee_id", "branch", "program", "group",
               "subject_name", "exam_type", "score", "date", "staff_name", "note", "created_at"],

    # صورة بروفيل (base64 صغير)
    "ProfilePics": ["phone", "trainee_id", "image_b64", "uploaded_at"],

    # دفوعات حسب السنة
    "Payments": ["payment_id", "trainee_id", "branch", "program", "group", "year"]
                + MONTHS + ["updated_at", "staff_name"],

    # Supports de cours: روابط Drive فقط
    "CourseLinks": ["link_id", "branch", "program", "group", "subject_name",
                    "title", "drive_share_url", "drive_view_url", "drive_download_url",
                    "uploaded_at", "staff_name"],

    # Planning: الموظف يكتب الحصص + لون
    "Timetable": ["row_id", "branch", "program", "group", "year",
                  "day", "start", "end",
                  "subject_name", "teacher", "room",
                  "color", "note",
                  "updated_at", "staff_name"],
}

# =========================================================
# UTILS
# =========================================================
def norm(x) -> str:
    return str(x or "").strip()

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def today_year_str() -> str:
    return str(datetime.now().year)

def df_filter(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    out = df.copy()
    for k, v in kwargs.items():
        if k in out.columns:
            out = out[out[k].astype(str).str.strip() == norm(v)]
    return out

def explain_api_error(e: APIError) -> str:
    try:
        status = getattr(e.response, "status_code", None)
        text = getattr(e.response, "text", "") or ""
        low = text.lower()
        if status == 429 or "quota" in low or "rate" in low:
            return "⚠️ 429 Quota (Google Sheets). جرّب Reboot واستنى شوية.\n" + text[:300]
        if status == 403 or "permission" in low or "forbidden" in low:
            return "❌ 403 Permission. لازم Share للـ Google Sheet للـ service account (client_email) كـ Editor.\n" + text[:300]
        if status == 404 or "not found" in low:
            return "❌ 404 Not found. تأكد GSHEET_ID صحيح + Share للـ service account.\n" + text[:300]
        return "❌ Google API Error:\n" + (text[:500] if text else str(e))
    except Exception:
        return "❌ Google API Error."

def compress_image_bytes(img_bytes: bytes, max_side: int = 256, quality: int = 70) -> bytes:
    im = Image.open(io.BytesIO(img_bytes))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    if im.mode == "RGBA":
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    w, h = im.size
    scale = min(max_side / max(w, h), 1.0)
    nw, nh = int(w * scale), int(h * scale)
    im = im.resize((nw, nh))
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()

# ---- Drive link helpers (manual links) ----
def extract_drive_file_id(url: str):
    u = norm(url)
    if not u:
        return None
    if "/file/d/" in u:
        try:
            return u.split("/file/d/")[1].split("/")[0]
        except Exception:
            return None
    if "open?id=" in u:
        try:
            return u.split("open?id=")[1].split("&")[0]
        except Exception:
            return None
    if "uc?id=" in u:
        try:
            return u.split("uc?id=")[1].split("&")[0]
        except Exception:
            return None
    m = re.search(r"/d/([a-zA-Z0-9_-]{10,})", u)
    if m:
        return m.group(1)
    return None

def to_view_and_download(share_url: str):
    fid = extract_drive_file_id(share_url)
    if not fid:
        return norm(share_url), norm(share_url)
    view_url = f"https://drive.google.com/file/d/{fid}/view"
    dl_url = f"https://drive.google.com/uc?export=download&id={fid}"
    return view_url, dl_url

def safe_url_md(label: str, url: str) -> str:
    u = norm(url)
    if not u:
        return ""
    return f"[{label}]({u})"

# =========================================================
# GOOGLE CLIENTS
# =========================================================
@st.cache_resource
def creds():
    creds_dict = st.secrets["gcp_service_account"]
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",  # ok even if only links
    ]
    return Credentials.from_service_account_info(creds_dict, scopes=scopes)

@st.cache_resource
def gs_client():
    return gspread.authorize(creds())

@st.cache_resource
def spreadsheet():
    return gs_client().open_by_key(st.secrets["GSHEET_ID"])

# =========================================================
# SCHEMA (SAFE, NO CLEAR)
# =========================================================
def ensure_headers_safe(ws, headers: list[str]):
    rng = ws.get("1:1")
    row1 = rng[0] if (rng and len(rng) > 0) else []
    row1 = [norm(x) for x in row1]

    if len(row1) == 0 or all(x == "" for x in row1):
        ws.append_row(headers, value_input_option="RAW")
        return

    if row1 != headers:
        st.warning(f"⚠️ Sheet '{ws.title}' headers مختلفة. ما عملتش مسح. إذا تحب صحّح الهيدرز يدويًا.")

def ensure_worksheets_and_headers():
    sh = spreadsheet()
    titles = [w.title for w in sh.worksheets()]
    for ws_name, headers in REQUIRED_SHEETS.items():
        if ws_name not in titles:
            sh.add_worksheet(title=ws_name, rows=4000, cols=max(16, len(headers) + 2))
            titles.append(ws_name)
        ws = sh.worksheet(ws_name)
        ensure_headers_safe(ws, headers)

def ensure_schema_once():
    # manual only to reduce 429
    if st.session_state.get("schema_ok", False):
        return
    if not st.session_state.get("init_schema_now", False):
        return
    try:
        ensure_worksheets_and_headers()
        st.session_state.schema_ok = True
        st.session_state.init_schema_now = False
        st.success("✅ Sheets vérifiées / initialisées (sans suppression).")
    except APIError as e:
        st.session_state.init_schema_now = False
        st.error(explain_api_error(e))
        raise

# =========================================================
# SHEETS CRUD (CACHED READ)
# =========================================================
@st.cache_data(ttl=300, show_spinner=False)
def read_df(ws_name: str) -> pd.DataFrame:
    ws = spreadsheet().worksheet(ws_name)
    values = ws.get_all_values()
    if len(values) <= 1:
        return pd.DataFrame(columns=REQUIRED_SHEETS[ws_name])
    headers = values[0]
    rows = values[1:]
    df = pd.DataFrame(rows, columns=headers)

    # ensure required columns exist (avoid KeyError)
    for c in REQUIRED_SHEETS[ws_name]:
        if c not in df.columns:
            df[c] = ""
    return df

def append_row(ws_name: str, row: dict):
    ws = spreadsheet().worksheet(ws_name)
    headers = REQUIRED_SHEETS[ws_name]
    out = [norm(row.get(h, "")) for h in headers]
    ws.append_row(out, value_input_option="USER_ENTERED")
    st.cache_data.clear()

def find_first_rownum_by_key(ws_name: str, key_cols: list[str], key_vals: list[str]):
    df = read_df(ws_name)
    if df.empty:
        return None
    m = df.copy()
    for c, v in zip(key_cols, key_vals):
        if c not in m.columns:
            return None
        m = m[m[c].astype(str).str.strip() == norm(v)]
    if m.empty:
        return None
    idx = int(m.index[0])
    return idx + 2  # sheet row number (1 header)

def update_row_by_key(ws_name: str, key_cols: list[str], key_vals: list[str], updates: dict) -> bool:
    row_num = find_first_rownum_by_key(ws_name, key_cols, key_vals)
    if row_num is None:
        return False
    ws = spreadsheet().worksheet(ws_name)
    headers = REQUIRED_SHEETS[ws_name]
    for col_name, val in updates.items():
        if col_name not in headers:
            continue
        ws.update_cell(row_num, headers.index(col_name) + 1, norm(val))
    st.cache_data.clear()
    return True

def delete_row_by_key(ws_name: str, key_cols: list[str], key_vals: list[str]) -> bool:
    row_num = find_first_rownum_by_key(ws_name, key_cols, key_vals)
    if row_num is None:
        return False
    ws = spreadsheet().worksheet(ws_name)
    ws.delete_rows(row_num)
    st.cache_data.clear()
    return True

# =========================================================
# PROFILE PICS
# =========================================================
def get_profile_pic_bytes(phone: str):
    df = read_df("ProfilePics")
    if df.empty:
        return None
    m = df[df["phone"].astype(str).str.strip() == norm(phone)]
    if m.empty:
        return None
    b64 = norm(m.iloc[0].get("image_b64"))
    if not b64:
        return None
    try:
        return base64.b64decode(b64.encode("utf-8"))
    except Exception:
        return None

def upsert_profile_pic(phone: str, trainee_id: str, img_bytes: bytes):
    small = compress_image_bytes(img_bytes, max_side=256, quality=70)
    b64 = base64.b64encode(small).decode("utf-8")
    updated = update_row_by_key(
        "ProfilePics",
        ["phone"], [phone],
        {"trainee_id": trainee_id, "image_b64": b64, "uploaded_at": now_str()},
    )
    if not updated:
        append_row("ProfilePics", {
            "phone": phone,
            "trainee_id": trainee_id,
            "image_b64": b64,
            "uploaded_at": now_str(),
        })

# =========================================================
# PAYMENTS
# =========================================================
def ensure_payment_row(trainee_id: str, branch: str, program: str, group: str, year: str, staff_name: str):
    df = read_df("Payments")
    if not df.empty:
        m = df[(df["trainee_id"].astype(str).str.strip() == norm(trainee_id)) &
               (df["year"].astype(str).str.strip() == norm(year))]
        if not m.empty:
            return

    row = {
        "payment_id": f"PAY-{uuid.uuid4().hex[:8].upper()}",
        "trainee_id": trainee_id,
        "branch": branch,
        "program": program,
        "group": group,
        "year": year,
        "updated_at": now_str(),
        "staff_name": staff_name,
    }
    for mo in MONTHS:
        row[mo] = "FALSE"
    append_row("Payments", row)

def set_payment_month(trainee_id: str, year: str, month: str, paid: bool, staff_name: str) -> bool:
    df = read_df("Payments")
    if df.empty:
        return False
    m = df[(df["trainee_id"].astype(str).str.strip() == norm(trainee_id)) &
           (df["year"].astype(str).str.strip() == norm(year))]
    if m.empty:
        return False
    idx = int(m.index[0])
    row_num = idx + 2
    ws = spreadsheet().worksheet("Payments")
    headers = REQUIRED_SHEETS["Payments"]
    ws.update_cell(row_num, headers.index(month) + 1, "TRUE" if paid else "FALSE")
    ws.update_cell(row_num, headers.index("updated_at") + 1, now_str())
    ws.update_cell(row_num, headers.index("staff_name") + 1, staff_name)
    st.cache_data.clear()
    return True

def list_payment_years(trainee_id: str) -> list[str]:
    df = read_df("Payments")
    if df.empty:
        return []
    m = df[df["trainee_id"].astype(str).str.strip() == norm(trainee_id)]
    years = sorted({norm(y) for y in m["year"].astype(str).tolist() if norm(y)})
    return years

# =========================================================
# TIMETABLE (PLANNING)
# =========================================================
def load_timetable(branch: str, program: str, group: str, year: str) -> pd.DataFrame:
    df = read_df("Timetable")
    if df.empty:
        return df
    df2 = df.copy()

    for c in REQUIRED_SHEETS["Timetable"]:
        if c not in df2.columns:
            df2[c] = ""

    m = (
        (df2["branch"].astype(str).str.strip() == norm(branch)) &
        (df2["program"].astype(str).str.strip() == norm(program)) &
        (df2["group"].astype(str).str.strip() == norm(group)) &
        (df2["year"].astype(str).str.strip() == norm(year))
    )
    df2 = df2[m].copy()

    df2["day_i"] = df2["day"].astype(str).map(lambda d: DAY_ORDER.get(norm(d), 99))
    df2["start_sort"] = df2["start"].astype(str)
    df2["end_sort"] = df2["end"].astype(str)
    df2 = df2.sort_values(by=["day_i", "start_sort", "end_sort"], ascending=True)
    return df2

def add_timetable_row(branch: str, program: str, group: str, year: str,
                      day: str, start: str, end: str,
                      subject_name: str, teacher: str, room: str,
                      color: str, note: str, staff_name: str):
    append_row("Timetable", {
        "row_id": f"TT-{uuid.uuid4().hex[:10].upper()}",
        "branch": branch,
        "program": program,
        "group": group,
        "year": year,
        "day": day,
        "start": start,
        "end": end,
        "subject_name": subject_name,
        "teacher": teacher,
        "room": room,
        "color": color,
        "note": note,
        "updated_at": now_str(),
        "staff_name": staff_name,
    })

def update_timetable_row(row_id: str, updates: dict) -> bool:
    updates = dict(updates)
    updates["updated_at"] = now_str()
    return update_row_by_key("Timetable", ["row_id"], [row_id], updates)

def delete_timetable_row(row_id: str) -> bool:
    return delete_row_by_key("Timetable", ["row_id"], [row_id])

def timetable_grid_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "<div style='padding:10px;border:1px dashed #999;border-radius:10px'>Aucun créneau enregistré.</div>"

    by_day = {d: [] for d in DAYS_FR}
    for _, r in df.iterrows():
        day = norm(r.get("day"))
        if day not in by_day:
            continue
        by_day[day].append(r.to_dict())

    html = """
    <style>
      .tt-wrap{width:100%; overflow-x:auto;}
      table.tt{border-collapse:separate;border-spacing:10px;width:100%;}
      table.tt th{font-size:14px;text-align:left;padding:6px 8px;}
      table.tt td{vertical-align:top;}
      .slot{
        border-radius:14px;
        padding:10px 12px;
        border:1px solid rgba(0,0,0,0.08);
        box-shadow:0 1px 2px rgba(0,0,0,0.06);
        margin-bottom:10px;
      }
      .slot .time{font-weight:700;}
      .slot .sub{font-weight:700;margin-top:6px;}
      .slot .meta{opacity:0.85;font-size:12px;margin-top:4px;}
    </style>
    <div class="tt-wrap">
    <table class="tt">
      <thead><tr>
    """
    for d in DAYS_FR:
        html += f"<th>{d}</th>"
    html += "</tr></thead><tbody><tr>"

    for d in DAYS_FR:
        html += "<td>"
        slots = by_day.get(d, [])
        if not slots:
            html += "<div style='opacity:0.6'>—</div>"
        else:
            for s in slots:
                color = norm(s.get("color")) or "#E8EEF7"
                stt = norm(s.get("start"))
                enn = norm(s.get("end"))
                sub = norm(s.get("subject_name"))
                teacher = norm(s.get("teacher"))
                room = norm(s.get("room"))
                note = norm(s.get("note"))
                meta = " | ".join([x for x in [teacher, room] if x])
                html += f"""
                <div class="slot" style="background:{color}">
                  <div class="time">{stt} → {enn}</div>
                  <div class="sub">{sub}</div>
                  <div class="meta">{meta}</div>
                  {"<div class='meta'>"+note+"</div>" if note else ""}
                </div>
                """
        html += "</td>"
    html += "</tr></tbody></table></div>"
    return html

# =========================================================
# GRADES CRUD (EDIT/DELETE)
# =========================================================
def update_grade_row(grade_id: str, updates: dict) -> bool:
    return update_row_by_key("Grades", ["grade_id"], [grade_id], updates)

def delete_grade_row(grade_id: str) -> bool:
    return delete_row_by_key("Grades", ["grade_id"], [grade_id])

# =========================================================
# AUTH / SESSION
# =========================================================
def ensure_session():
    st.session_state.setdefault("role", None)   # "staff" or None
    st.session_state.setdefault("user", {})
    st.session_state.setdefault("student", None)

def logout_staff():
    st.session_state.role = None
    st.session_state.user = {}

def staff_branch_login(branch: str, branch_password: str):
    df = read_df("Branches")
    if df.empty:
        return None
    df2 = df.copy()
    for c in ["branch", "staff_password", "is_active"]:
        if c not in df2.columns:
            df2[c] = ""
    df2["branch"] = df2["branch"].astype(str).str.strip()
    df2["staff_password"] = df2["staff_password"].astype(str).str.strip()
    df2["is_active"] = df2["is_active"].astype(str).str.strip().str.lower()

    m = df2[
        (df2["branch"] == norm(branch)) &
        (df2["staff_password"] == norm(branch_password)) &
        (df2["is_active"] != "false")
    ]
    if m.empty:
        return None
    return {"branch": norm(branch), "role": "staff"}

def student_login(phone: str, password: str):
    df = read_df("Accounts")
    if df.empty:
        return None
    for c in ["phone", "password"]:
        if c not in df.columns:
            df[c] = ""
    df2 = df.copy()
    df2["phone"] = df2["phone"].astype(str).str.strip()
    df2["password"] = df2["password"].astype(str).str.strip()
    m = df2[(df2["phone"] == norm(phone)) & (df2["password"] == norm(password))]
    if m.empty:
        return None
    return m.iloc[0].to_dict()

# =========================================================
# SIDEBAR STAFF LOGIN (LEFT)
# =========================================================
def sidebar_staff_login():
    st.sidebar.markdown("## 👨‍💼 Connexion Employé")

    branches_df = read_df("Branches")
    branches = []
    if not branches_df.empty and "branch" in branches_df.columns:
        branches = sorted([x for x in branches_df["branch"].astype(str).str.strip().unique().tolist() if x])

    if st.session_state.role == "staff":
        br = norm(st.session_state.user.get("branch"))
        st.sidebar.success(f"Connecté: {br}")

        st.sidebar.divider()
        st.sidebar.markdown("### 🧰 Maintenance")
        if st.sidebar.button("Initialiser / Vérifier les Sheets", use_container_width=True, key="sb_init"):
            st.session_state.init_schema_now = True
            st.rerun()

        if st.sidebar.button("Se déconnecter", use_container_width=True, key="sb_logout"):
            logout_staff()
            st.rerun()
        return

    if not branches:
        st.sidebar.warning("Branches vide. Ajoutez centres + mots de passe (Sheet: Branches).")
        return

    branch = st.sidebar.selectbox("Centre", branches, key="sb_branch")
    pwd = st.sidebar.text_input("Mot de passe du centre", type="password", key="sb_pwd")

    if st.sidebar.button("Connexion", use_container_width=True, key="sb_login"):
        user = staff_branch_login(branch, pwd)
        if user:
            st.session_state.role = "staff"
            st.session_state.user = user
            st.sidebar.success("✅ OK")
            st.rerun()
        else:
            st.sidebar.error("Mot de passe incorrect / centre inactif.")

# =========================================================
# STUDENT PORTAL (CENTER)
# =========================================================
def student_portal_center():
    st.markdown("## 🎓 Espace Stagiaire")

    tab1, tab2, tab3 = st.tabs(["🔐 Connexion", "🆕 Inscription", "📌 Mon espace"])

    # ------------------ Login
    with tab1:
        phone = st.text_input("Téléphone", key="stud_phone")
        pwd = st.text_input("Mot de passe", type="password", key="stud_pwd")
        if st.button("Se connecter", use_container_width=True, key="stud_login_btn"):
            acc = student_login(phone, pwd)
            if acc:
                update_row_by_key("Accounts", ["phone"], [phone], {"last_login": now_str()})
                st.session_state.student = acc
                st.success("✅ Connexion réussie")
                st.rerun()
            else:
                st.error("Téléphone / mot de passe incorrect.")
        if st.button("Se déconnecter", use_container_width=True, key="stud_logout_btn"):
            st.session_state.student = None
            st.rerun()

    # ------------------ Registration
    with tab2:
        st.subheader("Inscription (Nom libre + Téléphone لازم يكون مسجّل عند الإدارة)")

        branches_df = read_df("Branches")
        branches = sorted([x for x in branches_df.get("branch", pd.Series([], dtype=str)).astype(str).str.strip().unique().tolist() if x]) if not branches_df.empty else []
        if not branches:
            st.warning("Aucun centre disponible.")
            return

        b = st.selectbox("Centre", branches, key="reg_branch")

        prog_df = df_filter(read_df("Programs"), branch=b)
        if not prog_df.empty and "is_active" in prog_df.columns:
            prog_df = prog_df[prog_df["is_active"].astype(str).str.strip().str.lower() != "false"].copy()
        programs = sorted([x for x in prog_df.get("program_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])
        if not programs:
            st.warning("Aucune spécialité pour ce centre.")
            return
        p = st.selectbox("Spécialité", programs, key="reg_prog")

        grp_df = df_filter(read_df("Groups"), branch=b, program_name=p)
        if not grp_df.empty and "is_active" in grp_df.columns:
            grp_df = grp_df[grp_df["is_active"].astype(str).str.strip().str.lower() != "false"].copy()
        groups = sorted([x for x in grp_df.get("group_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])
        if not groups:
            st.warning("Aucun groupe.")
            return
        g = st.selectbox("Groupe", groups, key="reg_group")

        student_name = st.text_input("Nom (أي اسم تحب)", key="reg_name")
        phone = st.text_input("Téléphone (نفس رقمك عند الإدارة)", key="reg_phone")
        pwd = st.text_input("Mot de passe", type="password", key="reg_pwd")

        if st.button("Créer mon compte", use_container_width=True, key="reg_btn"):
            if not norm(student_name) or not norm(phone) or not norm(pwd):
                st.error("Nom + téléphone + mot de passe obligatoires.")
                return
            if len(norm(pwd)) < 4:
                st.error("Mot de passe قصير (min 4).")
                return

            acc = read_df("Accounts")
            if not acc.empty and "phone" in acc.columns and acc["phone"].astype(str).str.strip().eq(norm(phone)).any():
                st.error("Ce téléphone est déjà inscrit.")
                return

            tr = read_df("Trainees")
            if tr.empty:
                st.error("Aucun stagiaire enregistré.")
                return

            for c in ["branch", "program", "group", "phone", "trainee_id"]:
                if c not in tr.columns:
                    tr[c] = ""

            tr2 = tr.copy()
            for c in ["branch", "program", "group", "phone"]:
                tr2[c] = tr2[c].astype(str).str.strip()

            candidates = tr2[
                (tr2["branch"] == norm(b)) &
                (tr2["program"] == norm(p)) &
                (tr2["group"] == norm(g)) &
                (tr2["phone"] == norm(phone))
            ]
            if candidates.empty:
                st.error("رقم الهاتف موش موجود في Trainees لنفس centre/spécialité/groupe. الموظف لازم يسجل نفس الرقم.")
                return

            trainee_id = norm(candidates.iloc[0]["trainee_id"])
            append_row("Accounts", {
                "phone": norm(phone),
                "password": norm(pwd),
                "trainee_id": trainee_id,
                "student_name": norm(student_name),
                "created_at": now_str(),
                "last_login": ""
            })
            st.success("✅ Compte créé. امشي لصفحة Connexion.")

    # ------------------ My Space
    with tab3:
        acc = st.session_state.get("student")
        if not acc:
            st.info("اعمل Connexion باش تشوف النوطات والدفوعات والجدول والدروس.")
            return

        trainee_id = norm(acc.get("trainee_id"))
        phone = norm(acc.get("phone"))
        student_name = norm(acc.get("student_name"))

        tr = read_df("Trainees")
        if "trainee_id" not in tr.columns:
            tr["trainee_id"] = ""
        row = tr[tr["trainee_id"].astype(str).str.strip() == trainee_id].copy() if not tr.empty else pd.DataFrame()
        if row.empty:
            st.error("Compte مرتبط بمتربص غير موجود.")
            return

        info = row.iloc[0].to_dict()
        branch = norm(info.get("branch"))
        program = norm(info.get("program"))
        group = norm(info.get("group"))
        full_name_admin = norm(info.get("full_name"))

        c1, c2 = st.columns([1, 3])
        with c1:
            try:
                pic = get_profile_pic_bytes(phone)
            except APIError as e:
                st.error(explain_api_error(e))
                pic = None
            if pic:
                st.image(pic, caption="Photo de profil", use_container_width=True)
            else:
                st.info("Pas de photo")

        with c2:
            st.success(f"Bienvenue {student_name or full_name_admin} ✅")
            st.caption(f"Centre: {branch} | Spécialité: {program} | Groupe: {group} | Tél: {phone}")

            up = st.file_uploader("📸 Ajouter/Changer ma photo (PNG/JPG)", type=["png", "jpg", "jpeg"], key="pp_upl")
            if up is not None:
                img_bytes = up.read()
                st.image(img_bytes, caption="Aperçu", width=160)
                if st.button("Enregistrer ma photo", use_container_width=True, key="pp_save"):
                    try:
                        upsert_profile_pic(phone, trainee_id, img_bytes)
                        st.success("✅ Photo enregistrée.")
                        st.rerun()
                    except APIError as e:
                        st.error(explain_api_error(e))

        t1, t2, t3, t4 = st.tabs(["📝 Notes", "🗓️ Emploi du temps", "💳 Paiements", "📎 Supports"])

        with t1:
            gr = read_df("Grades")
            if "trainee_id" not in gr.columns:
                gr["trainee_id"] = ""
            grf = gr[gr["trainee_id"].astype(str).str.strip() == trainee_id].copy() if not gr.empty else pd.DataFrame()
            if grf.empty:
                st.info("Aucune note pour le moment.")
            else:
                for c in ["date", "created_at"]:
                    if c not in grf.columns:
                        grf[c] = ""
                grf = grf.sort_values(by=["date", "created_at"], ascending=False)
                cols_show = [c for c in ["subject_name", "exam_type", "score", "date", "staff_name", "note"] if c in grf.columns]
                st.dataframe(grf[cols_show], use_container_width=True, hide_index=True)

        with t2:
            y_default = today_year_str()
            tt_all = read_df("Timetable")
            if tt_all.empty:
                st.info("Aucun planning enregistré.")
            else:
                for c in ["branch", "program", "group", "year"]:
                    if c not in tt_all.columns:
                        tt_all[c] = ""
                years = sorted({norm(x) for x in tt_all[
                    (tt_all["branch"].astype(str).str.strip() == branch) &
                    (tt_all["program"].astype(str).str.strip() == program) &
                    (tt_all["group"].astype(str).str.strip() == group)
                ]["year"].astype(str).tolist() if norm(x)})
                if not years:
                    years = [y_default]
                year_pick = st.selectbox("Année", years, index=years.index(y_default) if y_default in years else 0, key="stud_tt_year")
                tt = load_timetable(branch, program, group, year_pick)
                components.html(timetable_grid_html(tt), height=520, scrolling=True)

        with t3:
            years = list_payment_years(trainee_id)
            if not years:
                st.info("لا توجد بيانات دفوعات.")
            else:
                y0 = today_year_str()
                year_pick = st.selectbox("Année", years, index=years.index(y0) if y0 in years else 0, key="stud_pay_year")
                pay = read_df("Payments")
                for c in ["trainee_id", "year"]:
                    if c not in pay.columns:
                        pay[c] = ""
                m = pay[
                    (pay["trainee_id"].astype(str).str.strip() == trainee_id) &
                    (pay["year"].astype(str).str.strip() == norm(year_pick))
                ] if not pay.empty else pd.DataFrame()
                if m.empty:
                    st.info("لا توجد بيانات دفوعات لهذه السنة.")
                else:
                    rowp = m.iloc[0].to_dict()
                    show = {mo: (norm(rowp.get(mo)).upper() == "TRUE") for mo in MONTHS}
                    st.dataframe(pd.DataFrame([show]), use_container_width=True, hide_index=True)

        with t4:
            files = read_df("CourseLinks")
            if files.empty:
                st.info("لا توجد دروس.")
            else:
                for c in ["branch", "program", "group"]:
                    if c not in files.columns:
                        files[c] = ""
                files = files[
                    (files["branch"].astype(str).str.strip() == branch) &
                    (files["program"].astype(str).str.strip() == program) &
                    (files["group"].astype(str).str.strip() == group)
                ].copy()
                if files.empty:
                    st.info("لا توجد دروس لهذه المجموعة.")
                else:
                    if "uploaded_at" not in files.columns:
                        files["uploaded_at"] = ""
                    files = files.sort_values(by=["uploaded_at"], ascending=False)
                    for _, r in files.iterrows():
                        title = norm(r.get("title")) or "Support"
                        subj = norm(r.get("subject_name"))
                        view_url = norm(r.get("drive_view_url"))
                        dl_url = norm(r.get("drive_download_url"))
                        share = norm(r.get("drive_share_url"))

                        st.markdown(f"### 📌 {subj}")
                        st.markdown(f"**{title}**")
                        links = []
                        if view_url:
                            links.append(safe_url_md("👀 Ouvrir", view_url))
                        elif share:
                            links.append(safe_url_md("👀 Ouvrir", share))
                        if dl_url:
                            links.append(safe_url_md("⬇️ Télécharger", dl_url))
                        if links:
                            st.markdown(" | ".join(links))
                        st.caption(f"Ajouté le: {norm(r.get('uploaded_at'))} — {norm(r.get('staff_name'))}")
                        st.divider()

# =========================================================
# STAFF WORK AREA (CENTER)
# =========================================================
def staff_work_center():
    st.markdown("## 🛠️ Espace Employé (Gestion)")

    if st.session_state.role != "staff":
        st.info("Connexion Employé من اليسار باش تفتح الإدارة.")
        return

    staff_branch = norm(st.session_state.user.get("branch"))
    staff_name = f"Staff-{staff_branch}"
    st.success(f"Centre: {staff_branch}")

    prog_df = df_filter(read_df("Programs"), branch=staff_branch)
    if not prog_df.empty and "is_active" in prog_df.columns:
        prog_df = prog_df[prog_df["is_active"].astype(str).str.strip().str.lower() != "false"].copy()
    programs = sorted([x for x in prog_df.get("program_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])

    colA, colB, colC = st.columns([2, 2, 1])
    with colA:
        program = st.selectbox("Spécialité", programs, key="mg_program") if programs else None
    with colB:
        group = None
        if program:
            grp_df = df_filter(read_df("Groups"), branch=staff_branch, program_name=program)
            if not grp_df.empty and "is_active" in grp_df.columns:
                grp_df = grp_df[grp_df["is_active"].astype(str).str.strip().str.lower() != "false"].copy()
            groups = sorted([x for x in grp_df.get("group_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])
            group = st.selectbox("Groupe", groups, key="mg_group") if groups else None
    with colC:
        year = st.selectbox("Année", [today_year_str(), str(int(today_year_str()) + 1), str(int(today_year_str()) - 1)], key="mg_year")

    tabs = st.tabs([
        "🏷️ Spécialités", "👥 Groupes", "📚 Matières",
        "👤 Stagiaires", "📝 Notes", "💳 Paiements",
        "🗓️ Planning", "📎 Supports"
    ])

    # -------- Programs
    with tabs[0]:
        cur = df_filter(read_df("Programs"), branch=staff_branch)
        show = cur[["program_name", "is_active", "created_at"]] if (not cur.empty and "program_name" in cur.columns) else cur
        st.dataframe(show, use_container_width=True, hide_index=True)

        new_prog = st.text_input("Nouvelle spécialité", key="new_prog")
        if st.button("Ajouter spécialité", use_container_width=True, key="add_prog_btn"):
            if not norm(new_prog):
                st.error("Nom obligatoire.")
            else:
                append_row("Programs", {
                    "program_id": f"PR-{uuid.uuid4().hex[:8].upper()}",
                    "branch": staff_branch,
                    "program_name": norm(new_prog),
                    "is_active": "true",
                    "created_at": now_str()
                })
                st.success("✅ Ajouté.")
                st.rerun()

    # -------- Groups
    with tabs[1]:
        if not program:
            st.info("اختار Spécialité من فوق.")
        else:
            cur = df_filter(read_df("Groups"), branch=staff_branch, program_name=program)
            show = cur[["group_name", "is_active", "created_at"]] if (not cur.empty and "group_name" in cur.columns) else cur
            st.dataframe(show, use_container_width=True, hide_index=True)

            new_group = st.text_input("Nouveau groupe", key="new_group")
            if st.button("Ajouter groupe", use_container_width=True, key="add_group_btn"):
                if not norm(new_group):
                    st.error("Nom obligatoire.")
                else:
                    append_row("Groups", {
                        "group_id": f"GP-{uuid.uuid4().hex[:8].upper()}",
                        "branch": staff_branch,
                        "program_name": norm(program),
                        "group_name": norm(new_group),
                        "is_active": "true",
                        "created_at": now_str()
                    })
                    st.success("✅ Ajouté.")
                    st.rerun()

    # -------- Subjects
    with tabs[2]:
        if not (program and group):
            st.info("اختار Spécialité + Groupe.")
        else:
            cur = df_filter(read_df("Subjects"), branch=staff_branch, program=program, group=group)
            show = cur[["subject_name", "is_active", "created_at"]] if (not cur.empty and "subject_name" in cur.columns) else cur
            st.dataframe(show, use_container_width=True, hide_index=True)

            subject_name = st.text_input("Nouvelle matière", key="new_subj")
            if st.button("Ajouter matière", use_container_width=True, key="add_subj_btn"):
                if not norm(subject_name):
                    st.error("Nom obligatoire.")
                else:
                    append_row("Subjects", {
                        "subject_id": f"SB-{uuid.uuid4().hex[:8].upper()}",
                        "branch": staff_branch,
                        "program": norm(program),
                        "group": norm(group),
                        "subject_name": norm(subject_name),
                        "is_active": "true",
                        "created_at": now_str()
                    })
                    st.success("✅ Ajouté.")
                    st.rerun()

    # -------- Trainees
    with tabs[3]:
        if not (program and group):
            st.info("اختار Spécialité + Groupe.")
        else:
            cur = df_filter(read_df("Trainees"), branch=staff_branch, program=program, group=group)
            show_cols = [c for c in ["full_name", "phone", "status", "created_at"] if c in cur.columns]
            st.dataframe(cur[show_cols] if not cur.empty else cur, use_container_width=True, hide_index=True)

            st.markdown("### ➕ Ajouter un stagiaire")
            name = st.text_input("Nom & Prénom", key="tr_name")
            phone = st.text_input("Téléphone (obligatoire pour inscription)", key="tr_phone")
            status = st.selectbox("Statut", ["active", "inactive"], key="tr_status")
            if st.button("Enregistrer stagiaire", use_container_width=True, key="tr_add_btn"):
                if not norm(name) or not norm(phone):
                    st.error("Nom + téléphone obligatoires.")
                else:
                    existing = df_filter(read_df("Trainees"), branch=staff_branch, program=program, group=group)
                    if not existing.empty and "phone" in existing.columns:
                        if existing["phone"].astype(str).str.strip().eq(norm(phone)).any():
                            st.error("Téléphone déjà موجود في نفس groupe.")
                            return
                    append_row("Trainees", {
                        "trainee_id": f"TR-{uuid.uuid4().hex[:8].upper()}",
                        "full_name": norm(name),
                        "phone": norm(phone),
                        "branch": staff_branch,
                        "program": norm(program),
                        "group": norm(group),
                        "status": status,
                        "created_at": now_str()
                    })
                    st.success("✅ Ajouté.")
                    st.rerun()

            st.divider()
            st.markdown("### 📥 Import Excel (xlsx) : colonnes = full_name + phone")
            up = st.file_uploader("Uploader Excel", type=["xlsx"], key="tr_excel")
            if up is not None:
                df = pd.read_excel(up)
                df.columns = [str(c).strip() for c in df.columns]
                st.dataframe(df.head(20), use_container_width=True)

                if st.button("✅ Importer maintenant", use_container_width=True, key="tr_do_import"):
                    if "full_name" not in df.columns or "phone" not in df.columns:
                        st.error("لازم colonnes: full_name و phone")
                    else:
                        existing = df_filter(read_df("Trainees"), branch=staff_branch, program=program, group=group)
                        existing_phones = set(existing.get("phone", pd.Series([], dtype=str)).astype(str).str.strip().tolist()) if not existing.empty else set()
                        count = 0
                        for _, r in df.iterrows():
                            fn = norm(r.get("full_name"))
                            ph = norm(r.get("phone"))
                            if not fn or not ph:
                                continue
                            if ph in existing_phones:
                                continue
                            append_row("Trainees", {
                                "trainee_id": f"TR-{uuid.uuid4().hex[:8].upper()}",
                                "full_name": fn,
                                "phone": ph,
                                "branch": staff_branch,
                                "program": norm(program),
                                "group": norm(group),
                                "status": "active",
                                "created_at": now_str(),
                            })
                            existing_phones.add(ph)
                            count += 1
                        st.success(f"✅ Import terminé: {count}")
                        st.rerun()

    # -------- Grades (ADD + LIST MY GRADES + EDIT/DELETE)
    with tabs[4]:
        if not (program and group):
            st.info("اختار Spécialité + Groupe.")
        else:
            tr = df_filter(read_df("Trainees"), branch=staff_branch, program=program, group=group)
            sub = df_filter(read_df("Subjects"), branch=staff_branch, program=program, group=group)
            if not sub.empty and "is_active" in sub.columns:
                sub = sub[sub["is_active"].astype(str).str.strip().str.lower() != "false"].copy()

            if tr.empty:
                st.warning("لا يوجد stagiaires.")
                return
            if sub.empty:
                st.warning("زيد matières قبل.")
                return

            st.markdown("### ➕ Ajouter une note")

            tr = tr.copy()
            for c in ["full_name", "phone", "trainee_id"]:
                if c not in tr.columns:
                    tr[c] = ""
            tr["label"] = tr["full_name"].astype(str) + " — " + tr["phone"].astype(str) + " — " + tr["trainee_id"].astype(str)
            chosen = st.selectbox("Stagiaire", tr["label"].tolist(), key="gr_tr_sel")
            trainee_id = norm(tr[tr["label"] == chosen].iloc[0]["trainee_id"])

            subjects = sorted([x for x in sub.get("subject_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])
            subject_name = st.selectbox("Matière", subjects, key="gr_sub_sel")
            exam_type = st.text_input("Type examen (DS1/TP/Examen...)", key="gr_exam")
            score = st.number_input("Note", min_value=0.0, max_value=20.0, value=10.0, step=0.25, key="gr_score")
            d = st.date_input("Date", value=datetime.now().date(), key="gr_date")
            note = st.text_area("Remarque", key="gr_note")

            if st.button("✅ Enregistrer la note", use_container_width=True, key="gr_save_btn"):
                if not norm(exam_type):
                    st.error("Type examen obligatoire.")
                else:
                    append_row("Grades", {
                        "grade_id": f"GR-{uuid.uuid4().hex[:8].upper()}",
                        "trainee_id": trainee_id,
                        "branch": staff_branch,
                        "program": norm(program),
                        "group": norm(group),
                        "subject_name": norm(subject_name),
                        "exam_type": norm(exam_type),
                        "score": str(score),
                        "date": str(d),
                        "staff_name": staff_name,
                        "note": norm(note),
                        "created_at": now_str(),
                    })
                    st.success("✅ Note enregistrée.")
                    st.rerun()

            st.divider()
            st.markdown("### 📋 Mes notes (حسب الاختصاص) — تعديل / حذف")

            gr = read_df("Grades")
            if gr.empty:
                st.info("Aucune note.")
                return

            for c in ["grade_id", "branch", "program", "group", "staff_name", "trainee_id",
                      "subject_name", "exam_type", "score", "date", "note", "created_at"]:
                if c not in gr.columns:
                    gr[c] = ""

            grf = gr[
                (gr["branch"].astype(str).str.strip() == staff_branch) &
                (gr["program"].astype(str).str.strip() == norm(program)) &
                (gr["group"].astype(str).str.strip() == norm(group)) &
                (gr["staff_name"].astype(str).str.strip() == norm(staff_name))
            ].copy()

            if grf.empty:
                st.info("ما عندك حتى note مسجلة لهالاختصاص/المجموعة.")
                return

            tr_map = {}
            tr_tmp = tr.copy()
            tr_tmp["trainee_id"] = tr_tmp["trainee_id"].astype(str).str.strip()
            tr_tmp["full_name"] = tr_tmp["full_name"].astype(str).str.strip()
            tr_map = dict(zip(tr_tmp["trainee_id"], tr_tmp["full_name"]))

            grf["trainee_name"] = grf["trainee_id"].astype(str).map(lambda x: tr_map.get(norm(x), ""))

            grf["date_sort"] = grf["date"].astype(str)
            grf["created_sort"] = grf["created_at"].astype(str)
            grf = grf.sort_values(by=["date_sort", "created_sort"], ascending=False)

            show_cols = [c for c in ["trainee_name", "subject_name", "exam_type", "score", "date", "note", "grade_id"] if c in grf.columns]
            st.dataframe(grf[show_cols], use_container_width=True, hide_index=True)

            st.divider()

            grf["label"] = (
                grf["trainee_name"].astype(str) + " | " +
                grf["subject_name"].astype(str) + " | " +
                grf["exam_type"].astype(str) + " | " +
                grf["score"].astype(str) + " | " +
                grf["date"].astype(str) + " | " +
                grf["grade_id"].astype(str)
            )

            pick = st.selectbox("اختر note للتعديل/الحذف", grf["label"].tolist(), key="gr_pick_edit")
            row = grf[grf["label"] == pick].iloc[0].to_dict()
            grade_id = norm(row.get("grade_id"))

            col1, col2 = st.columns(2)
            with col1:
                subject_e = st.text_input("Matière", value=norm(row.get("subject_name")), key="gr_subject_edit")
                exam_e = st.text_input("Type examen", value=norm(row.get("exam_type")), key="gr_exam_edit")
                try:
                    score_default = float(norm(row.get("score")) or 0)
                except Exception:
                    score_default = 0.0
                score_e = st.number_input("Note", min_value=0.0, max_value=20.0, value=score_default, step=0.25, key="gr_score_edit")

            with col2:
                try:
                    d0 = datetime.fromisoformat(norm(row.get("date"))).date()
                except Exception:
                    d0 = datetime.now().date()
                date_e = st.date_input("Date", value=d0, key="gr_date_edit")
                note_e = st.text_area("Remarque", value=norm(row.get("note")), key="gr_note_edit")

            csave, cdel = st.columns(2)
            with csave:
                if st.button("💾 Enregistrer modification", use_container_width=True, key="gr_update_btn"):
                    ok = update_grade_row(grade_id, {
                        "subject_name": norm(subject_e),
                        "exam_type": norm(exam_e),
                        "score": str(score_e),
                        "date": str(date_e),
                        "note": norm(note_e),
                        "staff_name": staff_name,
                    })
                    if ok:
                        st.success("✅ Note modifiée.")
                        st.rerun()
                    else:
                        st.error("❌ Échec (grade_id introuvable).")

            with cdel:
                if st.button("🗑️ Supprimer note", use_container_width=True, key="gr_delete_btn"):
                    ok = delete_grade_row(grade_id)
                    if ok:
                        st.success("✅ Note supprimée.")
                        st.rerun()
                    else:
                        st.error("❌ Échec suppression (grade_id introuvable).")

    # -------- Payments
    with tabs[5]:
        if not (program and group):
            st.info("اختار Spécialité + Groupe.")
        else:
            tr = df_filter(read_df("Trainees"), branch=staff_branch, program=program, group=group)
            if tr.empty:
                st.info("لا يوجد stagiaires.")
            else:
                tr = tr.copy()
                for c in ["full_name", "phone", "trainee_id"]:
                    if c not in tr.columns:
                        tr[c] = ""
                tr["label"] = tr["full_name"].astype(str) + " — " + tr["phone"].astype(str) + " — " + tr["trainee_id"].astype(str)
                chosen = st.selectbox("Choisir stagiaire", tr["label"].tolist(), key="pay_tr_sel")
                trainee_id = norm(tr[tr["label"] == chosen].iloc[0]["trainee_id"])

                ensure_payment_row(trainee_id, staff_branch, norm(program), norm(group), norm(year), staff_name)

                pay = read_df("Payments")
                for c in ["trainee_id", "year"]:
                    if c not in pay.columns:
                        pay[c] = ""
                m = pay[
                    (pay["trainee_id"].astype(str).str.strip() == trainee_id) &
                    (pay["year"].astype(str).str.strip() == norm(year))
                ].copy()
                rowp = m.iloc[0].to_dict() if not m.empty else {}

                cols = st.columns(4)
                for i, mo in enumerate(MONTHS):
                    paid = (norm(rowp.get(mo)).upper() == "TRUE")
                    with cols[i % 4]:
                        new_paid = st.checkbox(mo, value=paid, key=f"pay_{trainee_id}_{year}_{mo}")
                        if new_paid != paid:
                            set_payment_month(trainee_id, norm(year), mo, new_paid, staff_name)
                            st.rerun()

    # -------- Timetable (Planning CRUD)
    with tabs[6]:
        if not (program and group):
            st.info("اختار Spécialité + Groupe.")
        else:
            st.markdown("### 🗓️ Planning — Ajouter / Modifier / Supprimer")
            sub = df_filter(read_df("Subjects"), branch=staff_branch, program=program, group=group)
            if not sub.empty and "is_active" in sub.columns:
                sub = sub[sub["is_active"].astype(str).str.strip().str.lower() != "false"].copy()
            subjects = sorted([x for x in sub.get("subject_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])

            tt = load_timetable(staff_branch, norm(program), norm(group), norm(year))
            st.markdown("#### Aperçu (pour les stagiaires)")
            st.markdown(timetable_grid_html(tt), unsafe_allow_html=True)

            st.divider()
            c1, c2 = st.columns([1, 1])

            with c1:
                st.markdown("#### ➕ Ajouter un créneau")
                day = st.selectbox("Jour", DAYS_FR, key="tt_day_add")
                start = st.text_input("Heure début (ex: 18:00)", key="tt_start_add")
                end = st.text_input("Heure fin (ex: 19:30)", key="tt_end_add")
                teacher = st.text_input("Nom du formateur", key="tt_teacher_add")
                room = st.text_input("Salle (optionnel)", key="tt_room_add")
                color = st.color_picker("Couleur", value="#E8EEF7", key="tt_color_add")
                note = st.text_input("Note (optionnel)", key="tt_note_add")
                if subjects:
                    subject_name = st.selectbox("Matière", subjects, key="tt_subj_add")
                else:
                    subject_name = st.text_input("Matière (ajoute matières d'abord)", key="tt_subj_add_free")

                if st.button("✅ Ajouter", use_container_width=True, key="tt_add_btn"):
                    if not norm(day) or not norm(start) or not norm(end) or not norm(subject_name):
                        st.error("Jour + start + end + matière obligatoires.")
                    else:
                        add_timetable_row(
                            staff_branch, norm(program), norm(group), norm(year),
                            norm(day), norm(start), norm(end),
                            norm(subject_name), norm(teacher), norm(room),
                            norm(color), norm(note), staff_name
                        )
                        st.success("✅ Créneau ajouté.")
                        st.rerun()

            with c2:
                st.markdown("#### ✏️ Modifier / 🗑️ Supprimer")
                if tt.empty:
                    st.info("Aucun créneau.")
                else:
                    tt2 = tt.copy()
                    for c in ["row_id", "day", "start", "end", "subject_name", "teacher"]:
                        if c not in tt2.columns:
                            tt2[c] = ""
                    tt2["label"] = (
                        tt2["day"].astype(str) + " | " +
                        tt2["start"].astype(str) + "-" + tt2["end"].astype(str) + " | " +
                        tt2["subject_name"].astype(str) + " | " +
                        tt2["teacher"].astype(str) + " | " +
                        tt2["row_id"].astype(str)
                    )
                    pick = st.selectbox("Choisir un créneau", tt2["label"].tolist(), key="tt_pick")
                    row = tt2[tt2["label"] == pick].iloc[0].to_dict()
                    row_id = norm(row.get("row_id"))

                    day_e = st.selectbox("Jour", DAYS_FR, index=DAYS_FR.index(norm(row.get("day"))) if norm(row.get("day")) in DAYS_FR else 0, key="tt_day_edit")
                    start_e = st.text_input("Heure début", value=norm(row.get("start")), key="tt_start_edit")
                    end_e = st.text_input("Heure fin", value=norm(row.get("end")), key="tt_end_edit")
                    teacher_e = st.text_input("Formateur", value=norm(row.get("teacher")), key="tt_teacher_edit")
                    room_e = st.text_input("Salle", value=norm(row.get("room")), key="tt_room_edit")
                    color_e = st.color_picker("Couleur", value=norm(row.get("color")) or "#E8EEF7", key="tt_color_edit")
                    note_e = st.text_input("Note", value=norm(row.get("note")), key="tt_note_edit")

                    if subjects and norm(row.get("subject_name")) in subjects:
                        subj_e = st.selectbox("Matière", subjects, index=subjects.index(norm(row.get("subject_name"))), key="tt_subj_edit")
                    elif subjects:
                        subj_e = st.selectbox("Matière", subjects, key="tt_subj_edit_fallback")
                    else:
                        subj_e = st.text_input("Matière", value=norm(row.get("subject_name")), key="tt_subj_edit_free")

                    colx, coly = st.columns(2)
                    with colx:
                        if st.button("💾 Enregistrer modifications", use_container_width=True, key="tt_save_edit"):
                            ok = update_timetable_row(row_id, {
                                "day": norm(day_e),
                                "start": norm(start_e),
                                "end": norm(end_e),
                                "subject_name": norm(subj_e),
                                "teacher": norm(teacher_e),
                                "room": norm(room_e),
                                "color": norm(color_e),
                                "note": norm(note_e),
                                "staff_name": staff_name,
                            })
                            if ok:
                                st.success("✅ Modifié.")
                                st.rerun()
                            else:
                                st.error("❌ Échec modification (row_id introuvable).")

                    with coly:
                        if st.button("🗑️ Supprimer", use_container_width=True, key="tt_delete"):
                            ok = delete_timetable_row(row_id)
                            if ok:
                                st.success("✅ Supprimé.")
                                st.rerun()
                            else:
                                st.error("❌ Échec suppression (row_id introuvable).")

    # -------- Course links (Supports CRUD)
    with tabs[7]:
        if not (program and group):
            st.info("اختار Spécialité + Groupe.")
        else:
            st.markdown("### 📎 Supports de cours (liens Google Drive)")

            sub = df_filter(read_df("Subjects"), branch=staff_branch, program=program, group=group)
            if not sub.empty and "is_active" in sub.columns:
                sub = sub[sub["is_active"].astype(str).str.strip().str.lower() != "false"].copy()
            subjects = sorted([x for x in sub.get("subject_name", pd.Series([], dtype=str)).astype(str).str.strip().tolist() if x])

            if not subjects:
                st.warning("زيد matières قبل.")
                return

            subj = st.selectbox("Matière", subjects, key="cl_subj")
            title = st.text_input("Titre (ex: Cours 1, PDF, Exercice...)", key="cl_title")
            share_link = st.text_input("Lien Google Drive (Share: Anyone with the link)", key="cl_link")

            cadd, cdel = st.columns([1, 1])
            with cadd:
                if st.button("✅ Enregistrer", use_container_width=True, key="cl_save"):
                    if not norm(title) or not norm(share_link):
                        st.error("Titre + lien obligatoires.")
                    else:
                        view_url, dl_url = to_view_and_download(share_link)
                        append_row("CourseLinks", {
                            "link_id": f"CL-{uuid.uuid4().hex[:8].upper()}",
                            "branch": staff_branch,
                            "program": norm(program),
                            "group": norm(group),
                            "subject_name": norm(subj),
                            "title": norm(title),
                            "drive_share_url": norm(share_link),
                            "drive_view_url": view_url,
                            "drive_download_url": dl_url,
                            "uploaded_at": now_str(),
                            "staff_name": staff_name,
                        })
                        st.success("✅ Support enregistré.")
                        st.rerun()

            st.divider()
            files = read_df("CourseLinks")
            for c in ["branch", "program", "group"]:
                if c not in files.columns:
                    files[c] = ""
            files = files[
                (files["branch"].astype(str).str.strip() == staff_branch) &
                (files["program"].astype(str).str.strip() == norm(program)) &
                (files["group"].astype(str).str.strip() == norm(group))
            ].copy()

            if files.empty:
                st.info("Aucun support enregistré.")
            else:
                if "uploaded_at" not in files.columns:
                    files["uploaded_at"] = ""
                files = files.sort_values(by=["uploaded_at"], ascending=False)

                files["label"] = files.get("subject_name", "").astype(str) + " — " + files.get("title", "").astype(str) + " — " + files.get("link_id", "").astype(str)
                pick = st.selectbox("Choisir un support (pour supprimer)", files["label"].tolist(), key="cl_pick_del")
                link_id = norm(files[files["label"] == pick].iloc[0].get("link_id"))
                with cdel:
                    if st.button("🗑️ Supprimer ce support", use_container_width=True, key="cl_del"):
                        ok = delete_row_by_key("CourseLinks", ["link_id"], [link_id])
                        if ok:
                            st.success("✅ Supprimé.")
                            st.rerun()
                        else:
                            st.error("❌ Introuvable.")

                st.markdown("#### Liste des supports")
                st.dataframe(files[[c for c in ["subject_name", "title", "uploaded_at", "staff_name"] if c in files.columns]],
                             use_container_width=True, hide_index=True)

# =========================================================
# MAIN
# =========================================================
def main():
    ensure_session()
    ensure_schema_once()
    sidebar_staff_login()

    if st.session_state.role == "staff":
        staff_work_center()
        st.divider()
        student_portal_center()
    else:
        student_portal_center()
        st.divider()
        st.info("ℹ️ Connexion Employé موجودة في اليسار.")

if __name__ == "__main__":
    main()
