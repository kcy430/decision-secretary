"""
決策小秘書 V4.6
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
部署（Streamlit Community Cloud）：
  1. Push 整個資料夾（含 .streamlit/config.toml）到 GitHub
  2. share.streamlit.io 連接 repo，主檔選 app.py
  3. App settings → Secrets 加入：
       GEMINI_API_KEY = "AIza..."
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timedelta, date
import calendar as cal_lib
import google.generativeai as genai
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials as SACredentials
import json, tempfile, os, math, uuid

# ══════════════════════════════════════════════════════════════
#  PAGE CONFIG
# ══════════════════════════════════════════════════════════════
st.set_page_config(layout="wide", page_title="決策小秘書 V4.6", page_icon="🗓️")

st.markdown("""
<style>
    section[data-testid="stSidebar"] { background-color: #252525; }
    .block-container { padding-top: 3.5rem !important; padding-bottom: 1rem; }
    header[data-testid="stHeader"] { background-color: #1e1e1e; }
    [data-testid="stAppViewContainer"] { background-color: #1e1e1e; }
    /* 讓 dialog 背景深色 */
    [data-testid="stModal"] > div { background-color: #2a2a2a !important; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════
DAILY_CAP      = 4
COG_LOCK_AFTER = 3
GCAL_SCOPES    = ["https://www.googleapis.com/auth/calendar.events"]
LOAD_OPTIONS   = {"🔴 高負載": "high", "🟢 低負載": "low"}
LOAD_DISPLAY   = {v: k for k, v in LOAD_OPTIONS.items()}

RAG_DB = {
    "機構設計與3D列印": {
        "base_hours": 20, "diff": 3, "load": "high",
        "hardware": True,  "lead_days": 7,
        "roles": ["機械", "隊長", "所有人"],
    },
    "馬達控制演算法": {
        "base_hours": 35, "diff": 5, "load": "high",
        "hardware": False, "lead_days": 0,
        "roles": ["韌體", "電控"],
    },
    "影像辨識模組": {
        "base_hours": 40, "diff": 4, "load": "high",
        "hardware": False, "lead_days": 0,
        "roles": ["AI", "軟體"],
    },
    "企劃書與簡報撰寫": {
        "base_hours": 15, "diff": 2, "load": "low",
        "hardware": False, "lead_days": 0,
        "roles": ["隊長", "所有人"],
    },
    "PCB電路設計": {
        "base_hours": 25, "diff": 4, "load": "high",
        "hardware": True,  "lead_days": 14,
        "roles": ["電路", "硬體"],
    },
    "感測器整合": {
        "base_hours": 20, "diff": 3, "load": "high",
        "hardware": True,  "lead_days": 10,
        "roles": ["韌體", "電控", "所有人"],
    },
    "系統整合與測試": {
        "base_hours": 15, "diff": 3, "load": "high",
        "hardware": False, "lead_days": 0,
        "roles": ["隊長", "所有人"],
    },
}
TECH_TAGS = list(RAG_DB.keys())

# ══════════════════════════════════════════════════════════════
#  SESSION STATE INIT
# ══════════════════════════════════════════════════════════════
def _init():
    today = datetime.now()
    defaults = {
        "messages": [{"role": "assistant", "content":
            "你好！請在左側設定你的**參賽角色**，然後把比賽簡章丟進來讓我評估。\n\n"
            "點擊行事曆上的任意日期格子可以新增或編輯任務。"}],
        "schedule":       {},          # {date_str: {"tasks": [...], "hw_wait": None, "cog_locked": False}}
        "my_role":        "組員",
        "my_domains":     [],
        "team_size":      4,
        "weekly_checked": False,
        "gcal_service":   None,
        "calendar_id":    "primary",
        "days_passed":    0,
        "cal_year":       today.year,
        "cal_month":      today.month,
        "edit_date":      None,        # 目前正在編輯的日期字串
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ══════════════════════════════════════════════════════════════
#  TASK HELPERS
# ══════════════════════════════════════════════════════════════
def get_day(date_str: str) -> dict:
    """取得或建立某天的資料結構。"""
    if date_str not in st.session_state.schedule:
        st.session_state.schedule[date_str] = {
            "tasks": [], "hw_wait": None, "cog_locked": False
        }
    return st.session_state.schedule[date_str]

def blank_task() -> dict:
    return {
        "id":       str(uuid.uuid4())[:8],
        "name":     "",
        "load":     "high",
        "mine":     True,
        "deadline": None,   # date string YYYY-MM-DD or None
        "notes":    "",
    }

# ══════════════════════════════════════════════════════════════
#  COGNITIVE LOAD
# ══════════════════════════════════════════════════════════════
def _day_dominant_load(tasks: list) -> str:
    if not tasks:
        return "none"
    return "high" if any(t["load"] == "high" for t in tasks) else "low"

def recompute_cog_locks(schedule: dict) -> dict:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(120):
        ds = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        if ds not in schedule:
            schedule[ds] = {"tasks": [], "hw_wait": None, "cog_locked": False}
        if i < COG_LOCK_AFTER:
            schedule[ds]["cog_locked"] = False
            continue
        prev = [
            _day_dominant_load(
                schedule.get((today + timedelta(days=i - j - 1)).strftime("%Y-%m-%d"), {}).get("tasks", [])
            )
            for j in range(COG_LOCK_AFTER)
        ]
        schedule[ds]["cog_locked"] = all(l == "high" for l in prev)
    return schedule

# ══════════════════════════════════════════════════════════════
#  ROLE FILTER
# ══════════════════════════════════════════════════════════════
def filter_my_tasks(all_techs: list, my_role: str, my_domains: list) -> list:
    mine = []
    for tech in all_techs:
        meta = RAG_DB.get(tech)
        if not meta:
            continue
        roles = meta["roles"]
        if "所有人" in roles:
            mine.append(tech)
        elif my_role == "隊長" and "隊長" in roles:
            mine.append(tech)
        elif any(d in roles for d in my_domains):
            mine.append(tech)
    return mine

# ══════════════════════════════════════════════════════════════
#  ICS EXPORT（Apple / Samsung / Outlook 通用）
# ══════════════════════════════════════════════════════════════
def generate_ics(schedule: dict) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//決策小秘書 V4.6//ZH",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:決策小秘書",
        "X-WR-TIMEZONE:Asia/Taipei",
    ]
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    for date_str, day in schedule.items():
        tasks = day.get("tasks", [])
        if not tasks:
            continue
        # 每天合併成一個全天事件
        names   = list({t["name"] for t in tasks})
        notes   = "; ".join(t["notes"] for t in tasks if t.get("notes"))
        dl_list = [t["deadline"] for t in tasks if t.get("deadline")]
        dl_str  = "截止日: " + ", ".join(dl_list) if dl_list else ""

        dt_start = date_str.replace("-", "")
        # DTEND for all-day = next day
        dt_obj   = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)
        dt_end   = dt_obj.strftime("%Y%m%d")
        summary  = "🗓️ " + " | ".join(names)
        desc_parts = [p for p in [notes, dl_str] if p]
        desc     = "\\n".join(desc_parts) if desc_parts else "決策小秘書自動排入"

        uid = f"{date_str}-secretary@v46"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{dt_start}",
            f"DTEND;VALUE=DATE:{dt_end}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT",
        ]

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

# ══════════════════════════════════════════════════════════════
#  GOOGLE CALENDAR（保留架構供未來擴展）
# ══════════════════════════════════════════════════════════════
def build_gcal_service(sa_info: dict):
    creds = SACredentials.from_service_account_info(sa_info, scopes=GCAL_SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def write_to_gcal(service, tasks_by_date: dict, calendar_id: str = "primary") -> list:
    results = []
    for date_str, names in tasks_by_date.items():
        event = {
            "summary":     "🗓️ " + " | ".join(set(names)),
            "description": "由決策小秘書 V4.6 自動排入",
            "start": {"date": date_str},
            "end":   {"date": date_str},
        }
        try:
            service.events().insert(calendarId=calendar_id, body=event).execute()
            results.append(f"✅ {date_str}：{', '.join(set(names))}")
        except Exception as e:
            results.append(f"⚠️ {date_str} 失敗：{e}")
    return results

# ══════════════════════════════════════════════════════════════
#  EDIT DAY DIALOG
# ══════════════════════════════════════════════════════════════
@st.dialog("📅 編輯任務", width="large")
def edit_day_dialog(date_str: str):
    day  = get_day(date_str)
    dt   = datetime.strptime(date_str, "%Y-%m-%d")
    wd   = ["一","二","三","四","五","六","日"][dt.weekday()]
    st.markdown(f"#### {dt.strftime('%Y / %m / %d')} 週{wd}")

    if day["cog_locked"]:
        st.warning("🔒 這天已被認知負載系統鎖定，高負載名額已滿。仍可新增低負載任務。")

    tasks = day["tasks"]

    # ── 既有任務列表 ────────────────────────────────────────
    to_delete = []
    for i, task in enumerate(tasks):
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 2, 1])
            with c1:
                new_name = st.text_input(
                    "任務名稱", value=task["name"],
                    key=f"tname_{date_str}_{i}",
                    placeholder="輸入任務名稱"
                )
                task["name"] = new_name
            with c2:
                load_label = st.selectbox(
                    "負載類型",
                    list(LOAD_OPTIONS.keys()),
                    index=list(LOAD_OPTIONS.values()).index(task["load"]),
                    key=f"tload_{date_str}_{i}",
                )
                task["load"] = LOAD_OPTIONS[load_label]
            with c3:
                st.write("")
                st.write("")
                if st.button("🗑️", key=f"tdel_{date_str}_{i}", help="刪除此任務"):
                    to_delete.append(i)

            c4, c5 = st.columns([2, 3])
            with c4:
                dl_val = date.fromisoformat(task["deadline"]) if task.get("deadline") else None
                new_dl = st.date_input(
                    "截止日（選填）",
                    value=dl_val,
                    key=f"tdl_{date_str}_{i}",
                    format="YYYY/MM/DD",
                )
                task["deadline"] = new_dl.isoformat() if new_dl else None
            with c5:
                new_notes = st.text_input(
                    "備註（選填）", value=task.get("notes", ""),
                    key=f"tnotes_{date_str}_{i}",
                    placeholder="備忘、提醒、連結…"
                )
                task["notes"] = new_notes

    # 執行刪除（反向刪避免 index shift）
    for i in reversed(to_delete):
        tasks.pop(i)

    st.divider()

    # ── 新增任務 ────────────────────────────────────────────
    st.markdown("**＋ 新增任務**")
    nc1, nc2 = st.columns([4, 2])
    with nc1:
        new_name = st.text_input("任務名稱", key=f"new_name_{date_str}", placeholder="例如：韌體架構規劃")
    with nc2:
        new_load_label = st.selectbox("負載類型", list(LOAD_OPTIONS.keys()), key=f"new_load_{date_str}")

    nc3, nc4 = st.columns([2, 3])
    with nc3:
        new_dl = st.date_input("截止日（選填）", value=None, key=f"new_dl_{date_str}", format="YYYY/MM/DD")
    with nc4:
        new_notes = st.text_input("備註（選填）", key=f"new_notes_{date_str}", placeholder="備忘、提醒、連結…")

    if st.button("＋ 加入", type="primary", use_container_width=True):
        if new_name.strip():
            tasks.append({
                "id":       str(uuid.uuid4())[:8],
                "name":     new_name.strip(),
                "load":     LOAD_OPTIONS[new_load_label],
                "mine":     True,
                "deadline": new_dl.isoformat() if new_dl else None,
                "notes":    new_notes.strip(),
            })
            st.success(f"已新增「{new_name.strip()}」")
        else:
            st.warning("請輸入任務名稱")

    st.divider()

    # ── 備料等待標記 ────────────────────────────────────────
    hw_val = day.get("hw_wait") or ""
    new_hw = st.text_input(
        "⏳ 備料等待標記（選填）",
        value=hw_val,
        key=f"hw_{date_str}",
        placeholder="例如：備料：PCB電路設計"
    )
    day["hw_wait"] = new_hw.strip() or None

    # ── 儲存 ────────────────────────────────────────────────
    st.write("")
    if st.button("✅ 儲存並關閉", type="primary", use_container_width=True):
        recompute_cog_locks(st.session_state.schedule)
        st.session_state.edit_date = None
        st.rerun()

# ══════════════════════════════════════════════════════════════
#  CALENDAR RENDERER
# ══════════════════════════════════════════════════════════════
def render_month_html(schedule: dict, year: int, month: int) -> str:
    today_date = datetime.now().date()
    num_days   = cal_lib.monthrange(year, month)[1]
    first_wd   = datetime(year, month, 1).weekday()

    css = """
<style>
* { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 0; }
body { background: #1e1e1e; color: #e0e0e0; }
.cal-wrap { padding: 4px 2px; }
.cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 5px; }
.day-header { text-align: center; font-size: 0.72em; font-weight: 600; color: #666; padding: 4px 0 6px; letter-spacing: 0.05em; }
.day-cell {
    background: #2a2a2a;
    border: 1px solid #363636;
    border-radius: 8px;
    min-height: 72px;
    padding: 5px;
    display: flex;
    flex-direction: column;
    cursor: pointer;
    transition: border-color .15s, background .15s;
}
.day-cell:hover { border-color: #4dabf7; background: #2f3640; }
.day-cell.empty   { background: transparent; border: none; cursor: default; min-height: 72px; }
.day-cell.empty:hover { background: transparent; border: none; }
.day-cell.today   { border: 1.5px solid #4dabf7; }
.day-cell.weekend { background: #252525; }
.day-cell.cog-lock { border: 1.5px solid #ff6b6b !important; background: #2e2020; }
.day-cell.hw-wait  { border: 1.5px dashed #ffa94d !important; background: #2c2618; }
.day-num { font-size: 0.78em; font-weight: 600; color: #bbb; margin-bottom: 3px; }
.day-num.is-today   { color: #4dabf7; }
.day-num.is-weekend { color: #777; }
.day-sub { font-size: 0.60em; font-weight: 700; letter-spacing: 0.04em; margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sub-lock { color: #ff6b6b; }
.sub-wait { color: #ffa94d; }
.task-tag { font-size: 0.68em; padding: 1px 5px; border-radius: 4px; margin-bottom: 2px; display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tag-h    { background: rgba(255,107,107,0.18); color: #ff8787; }
.tag-l    { background: rgba(81,207,102,0.18);  color: #69db7c; }
.tag-mine { border-left: 2px solid #4dabf7; padding-left: 4px; }
.has-dl   { font-size: 0.58em; color: #aaa; margin-top: 1px; }
.add-hint { font-size: 0.62em; color: #555; margin-top: auto; padding-top: 4px; }
.legend   { display: flex; gap: 14px; flex-wrap: wrap; margin-top: 8px; padding: 0 2px; font-size: 0.68em; color: #666; }
</style>
"""

    html = css + '<div class="cal-wrap"><div class="cal-grid">'

    for wd in ["一", "二", "三", "四", "五", "六", "日"]:
        html += f'<div class="day-header">週{wd}</div>'

    for _ in range(first_wd):
        html += '<div class="day-cell empty"></div>'

    for day in range(1, num_days + 1):
        dt = datetime(year, month, day)
        ds = dt.strftime("%Y-%m-%d")
        d  = schedule.get(ds, {"tasks": [], "hw_wait": None, "cog_locked": False})

        is_today   = dt.date() == today_date
        is_weekend = dt.weekday() >= 5

        cell_cls = "day-cell"
        if is_weekend:          cell_cls += " weekend"
        if is_today:            cell_cls += " today"
        if d.get("cog_locked"): cell_cls += " cog-lock"
        elif d.get("hw_wait"):  cell_cls += " hw-wait"

        num_cls = "day-num"
        if is_today:   num_cls += " is-today"
        if is_weekend: num_cls += " is-weekend"

        sub_html = ""
        if d.get("cog_locked"):
            sub_html = '<div class="day-sub sub-lock">🔒 認知鎖定</div>'
        elif d.get("hw_wait"):
            label = d["hw_wait"][:10] + ("…" if len(d["hw_wait"]) > 10 else "")
            sub_html = f'<div class="day-sub sub-wait">⏳ {label}</div>'

        shown: dict = {}
        for t in d.get("tasks", []):
            n = t["name"]
            if n not in shown:
                shown[n] = {"count": 0, "load": t["load"], "mine": t.get("mine", False), "deadline": t.get("deadline")}
            shown[n]["count"] += 1

        tags_html = ""
        for name, info in shown.items():
            lc  = "tag-h" if info["load"] == "high" else "tag-l"
            mc  = " tag-mine" if info["mine"] else ""
            lbl = name + (f" ×{info['count']}" if info["count"] > 1 else "")
            dl_hint = f'<div class="has-dl">📅 截止 {info["deadline"]}</div>' if info.get("deadline") else ""
            tags_html += f'<span class="task-tag {lc}{mc}">{lbl}</span>{dl_hint}'

        # 點擊提示（空天才顯示）
        add_hint = '<div class="add-hint">＋ 點擊新增</div>' if not d.get("tasks") else ""

        # onclick 傳日期給父層
        onclick = f"window.parent.postMessage({{type:'edit_date',date:'{ds}'}}, '*')"
        html += (
            f'<div class="{cell_cls}" onclick="{onclick}">'
            f'<div class="{num_cls}">{day}</div>'
            f'{sub_html}{tags_html}{add_hint}'
            f'</div>'
        )

    html += "</div>"
    html += """
<div class="legend">
    <span style="color:#ff8787">■ 高負載</span>
    <span style="color:#69db7c">■ 低負載</span>
    <span style="color:#ffa94d">■ 備料等待</span>
    <span style="border-left:2px solid #4dabf7;padding-left:4px;color:#4dabf7;">藍邊 = 我的任務</span>
    <span style="color:#ff6b6b">🔒 認知鎖定</span>
    <span style="color:#4dabf7">● 今天</span>
    <span style="color:#555">點擊格子可編輯</span>
</div>
</div>"""

    # postMessage → Streamlit via query param（讓點擊格子能觸發 dialog）
    html += """
<script>
window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'edit_date') {
        window.parent.postMessage(e.data, '*');
    }
});
</script>"""

    return html

# ══════════════════════════════════════════════════════════════
#  AI BRAIN
# ══════════════════════════════════════════════════════════════
class SecretaryBrain:

    @staticmethod
    def loop_1_parse(user_input: str, uploaded_file=None) -> dict:
        generation_config = genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "tech_tags":           {"type": "ARRAY", "items": {"type": "STRING"},
                                           "description": f"只能從以下挑選：{TECH_TAGS}"},
                    "confidence":          {"type": "STRING",
                                           "description": "資訊充足填 high；不明或無死線填 low"},
                    "follow_up_question":  {"type": "STRING",
                                           "description": "若 low，提出問題詢問缺少的資訊"},
                    "deadline_days":       {"type": "INTEGER",
                                           "description": "距死線天數，未提及預設 14"},
                    "project_name":        {"type": "STRING",
                                           "description": "專案名稱，找不到填「未命名專案」"},
                },
                "required": ["tech_tags","confidence","follow_up_question","deadline_days","project_name"],
            },
        )
        model     = genai.GenerativeModel("gemini-2.5-flash")
        parts     = [f"請分析以下專案需求或比賽簡章，抽取關鍵資訊：\n{user_input}"]
        temp_path = None

        if uploaded_file:
            ext = "." + uploaded_file.name.split(".")[-1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                f.write(uploaded_file.getvalue())
                temp_path = f.name
            try:
                parts.append(genai.upload_file(temp_path))
            except Exception as e:
                if temp_path and os.path.exists(temp_path): os.remove(temp_path)
                return {"status": "error", "reply": f"⚠️ 檔案上傳失敗：`{e}`"}

        try:
            resp = model.generate_content(parts, generation_config=generation_config)
            data = json.loads(resp.text)
        except Exception as e:
            data = {"confidence": "low", "follow_up_question": f"API 錯誤：`{e}`",
                    "tech_tags": [], "deadline_days": 14, "project_name": "未命名專案"}
        finally:
            if temp_path and os.path.exists(temp_path): os.remove(temp_path)

        if data.get("confidence") == "low" or not data.get("tech_tags"):
            return {"status": "needs_info",
                    "reply": f"🛑 **資訊不足，已阻斷排程。**\n\n{data.get('follow_up_question')}"}
        return {"status": "success", **data}

    @staticmethod
    def sandbox(my_techs: list, current_schedule: dict, deadline_days: int) -> dict:
        today    = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deadline = min(deadline_days, 120)

        sch = {
            ds: {"tasks": list(v["tasks"]), "hw_wait": v["hw_wait"], "cog_locked": v["cog_locked"]}
            for ds, v in current_schedule.items()
        }
        recompute_cog_locks(sch)

        hw_warnings = []
        for tech in my_techs:
            meta = RAG_DB.get(tech, {})
            if meta.get("hardware") and meta.get("lead_days", 0) > 0:
                lead = min(meta["lead_days"], deadline - 1)
                for i in range(lead):
                    ds = (today + timedelta(days=i)).strftime("%Y-%m-%d")
                    if ds not in sch:
                        sch[ds] = {"tasks": [], "hw_wait": None, "cog_locked": False}
                    if sch[ds]["hw_wait"] is None:
                        sch[ds]["hw_wait"] = f"備料：{tech}"
                hw_warnings.append(
                    f"⏳ **{tech}** 需備料 {meta['lead_days']} 天，前 {lead} 天標記為等待期。")

        total_hours = sum(RAG_DB[t]["base_hours"] for t in my_techs if t in RAG_DB)
        max_diff    = max((RAG_DB[t]["diff"] for t in my_techs if t in RAG_DB), default=1)
        weighted    = math.ceil(total_hours * (1 + (max_diff - 1) * 0.25))

        task_queue = []
        for tech in my_techs:
            meta = RAG_DB.get(tech, {})
            w    = math.ceil(meta.get("base_hours", 4) * (1 + (meta.get("diff", 1) - 1) * 0.25))
            task_queue.extend([
                {"id": str(uuid.uuid4())[:8], "name": tech,
                 "load": meta.get("load","high"), "mine": True,
                 "deadline": None, "notes": ""}
            ] * w)

        tasks_by_date: dict = {}
        remaining = list(task_queue)

        for i in range(deadline - 1, -1, -1):
            if not remaining: break
            ds = (today + timedelta(days=i)).strftime("%Y-%m-%d")
            if ds not in sch:
                sch[ds] = {"tasks": [], "hw_wait": None, "cog_locked": False}
            avail = DAILY_CAP - len(sch[ds]["tasks"])
            if avail <= 0: continue
            placed, new_rem = 0, []
            for task in remaining:
                if placed >= avail: new_rem.append(task); continue
                if sch[ds]["cog_locked"] and task["load"] == "high": new_rem.append(task); continue
                sch[ds]["tasks"].append(task)
                tasks_by_date.setdefault(ds, []).append(task["name"])
                placed += 1
            remaining = new_rem

        recompute_cog_locks(sch)
        cog_lock_dates = [
            (today + timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(deadline)
            if sch.get((today + timedelta(days=i)).strftime("%Y-%m-%d"), {}).get("cog_locked")
        ]

        return {
            "schedule": sch, "weighted_hours": weighted,
            "tasks_by_date": tasks_by_date, "overflow": bool(remaining),
            "hw_warnings": hw_warnings, "cog_lock_dates": cog_lock_dates,
        }

    @staticmethod
    def loop_2_strategy(parse_data: dict, my_techs: list, sandbox_res: dict) -> str:
        project   = parse_data.get("project_name", "此專案")
        all_techs = parse_data["tech_tags"]
        not_mine  = [t for t in all_techs if t not in my_techs]
        w         = sandbox_res["weighted_hours"]
        deadline  = parse_data["deadline_days"]

        lines = [
            f"### 📋 {project}",
            f"**全專案技術棧**：{', '.join(all_techs)}",
            f"**你的負責項目**（{len(my_techs)} 項）："
            f"{', '.join(my_techs) if my_techs else '⚠️ 無匹配，請更新左側角色設定'}",
        ]
        if not_mine:
            lines.append(f"**隊友負責**（{len(not_mine)} 項）：{', '.join(not_mine)}")
        lines.append("---")

        if not my_techs:
            lines.append("你的角色與這場比賽的技術棧沒有交集，請在左側更新「我的領域」。")
            return "\n".join(lines)

        for w_msg in sandbox_res["hw_warnings"]:
            lines.append(w_msg)
        if sandbox_res["cog_lock_dates"]:
            sample = ", ".join(sandbox_res["cog_lock_dates"][:3])
            lines.append(f"🧠 **認知負載警告**：已鎖定 {sample} 等日期的高負載名額。")

        lines.append("")
        max_daily = DAILY_CAP * deadline
        load_pct  = int(w / max_daily * 100) if max_daily > 0 else 999

        if sandbox_res["overflow"]:
            lines.append(f"### 🔴 無法接案\n加權工時 **{w} 小時**，死線前物理時間已擊穿。\n"
                         f"建議：延後死線 / 縮減功能 / 增加人手。")
        elif load_pct >= 80:
            lines.append(f"### 🟡 勉強可接（中等風險）\n加權工時 **{w} 小時**，行事曆負載達 {load_pct}%，已排入。\n"
                         f"建議：本週末先完成環境架設，把風險前壓。")
        else:
            lines.append(f"### 🟢 適合接案\n加權工時 **{w} 小時**，行事曆負載 {load_pct}%，餘裕充足，已自動排入。")

        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  SIDEBAR
# ══════════════════════════════════════════════════════════════
with st.sidebar:

    st.header("🔑 系統狀態")
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        st.success("🧠 大腦已連線")
    except KeyError:
        st.error("找不到 GEMINI_API_KEY，請到 Streamlit Cloud → App settings → Secrets 設定。")
        api_key = None
        st.stop()

    st.divider()

    st.header("👤 我的角色設定")
    c1, c2 = st.columns(2)
    with c1:
        st.selectbox("比賽角色", ["組員", "隊長"], key="my_role")
    with c2:
        st.number_input("隊伍人數", min_value=1, max_value=10, key="team_size")

    st.multiselect(
        "我負責的技術領域",
        ["韌體", "電控", "機械", "AI", "軟體", "電路", "硬體"],
        key="my_domains",
        help="決定哪些任務會排進你的行事曆",
    )
    if st.session_state.my_role == "隊長":
        st.caption("隊長自動負責：企劃書、系統整合，以及你選擇的領域。")

    st.divider()

    # ── ICS 匯出 ───────────────────────────────────────────
    st.header("📤 匯出行事曆")
    st.caption("匯出 .ics 檔案可匯入 Apple 行事曆、Samsung 行事曆、Outlook 等。")
    ics_data = generate_ics(st.session_state.schedule)
    st.download_button(
        label="⬇️ 下載 ICS 檔案",
        data=ics_data,
        file_name="secretary_schedule.ics",
        mime="text/calendar",
        use_container_width=True,
        help="下載後：iOS 直接點擊 → 加入行事曆；Android 用行事曆 app 開啟匯入",
    )

    st.divider()

    st.header("⏳ 進度防呆")
    st.slider("快轉天數（測試用）", 0, 14, key="days_passed")
    if st.session_state.days_passed >= 7 and not st.session_state.weekly_checked:
        st.error("🚨 **系統強制鎖死**\n超過 7 天未更新進度。")
        report = st.text_area("請回報本週進度以解鎖：")
        if st.button("提交解鎖"):
            if report.strip():
                st.session_state.weekly_checked = True
                st.rerun()
            else:
                st.warning("請輸入實際進度後再提交。")
        st.stop()

# ══════════════════════════════════════════════════════════════
#  DIALOG TRIGGER（從月曆格子點擊觸發）
# ══════════════════════════════════════════════════════════════
# 由於 iframe postMessage 無法直接觸發 Streamlit rerun，
# 改用側邊欄的日期選擇器作為穩定的觸發入口
with st.sidebar:
    st.divider()
    st.header("✏️ 手動選擇日期編輯")
    selected = st.date_input(
        "選擇要編輯的日期",
        value=None,
        key="sidebar_date_picker",
        format="YYYY/MM/DD",
        help="也可直接點月曆格子選擇",
    )
    if selected:
        if st.button("開啟編輯視窗", use_container_width=True, type="primary"):
            st.session_state.edit_date = selected.isoformat()
            st.rerun()

# 觸發 dialog
if st.session_state.edit_date:
    edit_day_dialog(st.session_state.edit_date)

# ══════════════════════════════════════════════════════════════
#  MAIN LAYOUT
# ══════════════════════════════════════════════════════════════
col_cal, col_chat = st.columns([7, 3])

# ── 月曆 ──────────────────────────────────────────────────────
with col_cal:
    yr = st.session_state.cal_year
    mo = st.session_state.cal_month

    nav_l, nav_title, nav_r = st.columns([1, 4, 1])
    with nav_l:
        if st.button("◀", use_container_width=True, help="上個月"):
            st.session_state.cal_month = 12 if mo == 1 else mo - 1
            if mo == 1: st.session_state.cal_year -= 1
            st.rerun()
    with nav_title:
        st.markdown(
            f"<h3 style='text-align:center;margin:0;padding:4px 0'>🗓️ {yr} 年 {mo} 月</h3>",
            unsafe_allow_html=True,
        )
    with nav_r:
        if st.button("▶", use_container_width=True, help="下個月"):
            st.session_state.cal_month = 1 if mo == 12 else mo + 1
            if mo == 12: st.session_state.cal_year += 1
            st.rerun()

    num_days   = cal_lib.monthrange(yr, mo)[1]
    first_wd   = datetime(yr, mo, 1).weekday()
    num_rows   = math.ceil((first_wd + num_days) / 7)
    cal_height = num_rows * 82 + 100

    components.html(
        render_month_html(st.session_state.schedule, yr, mo),
        height=cal_height,
        scrolling=False,
    )

# ── 聊天 ──────────────────────────────────────────────────────
with col_chat:
    st.header("🧠 智慧推論大腦")

    with st.expander("📂 上傳簡章（PDF / PNG / JPG）", expanded=False):
        uploaded_file = st.file_uploader(
            "拖曳或選擇檔案",
            type=["pdf", "png", "jpg", "jpeg"],
            label_visibility="collapsed",
        )

    chat_box = st.container(height=300)
    with chat_box:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if prompt := st.chat_input("輸入指令，例如：幫我評估這個比賽簡章"):
        if not api_key:
            st.error("API Key 未設定。")
            st.stop()

        st.session_state.messages.append({"role": "user", "content": prompt})

        with chat_box:
            with st.chat_message("user"):
                st.markdown(prompt)

            with st.chat_message("assistant"):
                with st.spinner("🔍 大腦解析中..."):
                    parsed = SecretaryBrain.loop_1_parse(prompt, uploaded_file)

                if parsed["status"] != "success":
                    reply = parsed["reply"]
                    st.markdown(reply)
                else:
                    my_techs = filter_my_tasks(
                        parsed["tech_tags"],
                        st.session_state.my_role,
                        st.session_state.my_domains,
                    )
                    with st.spinner("⚙️ 沙盒模擬排程中..."):
                        sim = SecretaryBrain.sandbox(
                            my_techs, st.session_state.schedule, parsed["deadline_days"]
                        )

                    reply = SecretaryBrain.loop_2_strategy(parsed, my_techs, sim)
                    st.markdown(reply)

                    if not sim["overflow"] and my_techs:
                        st.session_state.schedule = sim["schedule"]
                        recompute_cog_locks(st.session_state.schedule)
                        # ICS 自動更新（下次點下載時就是最新版）

                st.session_state.messages.append({"role": "assistant", "content": reply})

        st.rerun()
