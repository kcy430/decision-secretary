"""
決策小秘書 V4.6
部署：push 到 GitHub → share.streamlit.io → Secrets 填 GEMINI_API_KEY
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

    /* ── 月曆格子：key 以 cal_ 開頭的按鈕 ── */
    button[kind="secondary"][data-testid="baseButton-secondary"] {
        width: 100% !important;
    }
    [data-testid="stButton"]:has(button[key^="cal_"]) button,
    .cal-btn button {
        width: 100% !important;
        min-height: 82px !important;
        text-align: left !important;
        align-items: flex-start !important;
        justify-content: flex-start !important;
        padding: 7px 9px !important;
        background-color: #2a2a2a !important;
        border: 1px solid #363636 !important;
        border-radius: 8px !important;
        color: #ccc !important;
        font-size: 0.80em !important;
        white-space: pre-wrap !important;
        line-height: 1.55 !important;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important;
        transition: border-color .15s, background .15s !important;
    }
    .cal-btn button:hover {
        border-color: #4dabf7 !important;
        background-color: #263040 !important;
        color: #e8e8e8 !important;
    }
    .cal-today button {
        border: 1.5px solid #4dabf7 !important;
        color: #93c5fd !important;
    }
    .cal-weekend button {
        background-color: #242424 !important;
    }
    .cal-coglock button {
        border: 1.5px solid #ff6b6b !important;
        background-color: #2e1f1f !important;
    }
    .cal-hwwait button {
        border: 1.5px dashed #ffa94d !important;
        background-color: #2b2215 !important;
    }
    /* 空格子 */
    .cal-empty {
        min-height: 82px;
        border-radius: 8px;
        background: #1a1a1a;
        border: 1px solid #2a2a2a;
    }
    /* 週標題 */
    .cal-header {
        text-align: center;
        font-size: 0.72em;
        font-weight: 600;
        color: #555;
        padding: 2px 0 8px;
        letter-spacing: 0.06em;
    }
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

# RAG_DB 已移除，改由 Gemini 自由推論任意任務的工時與負載

# ══════════════════════════════════════════════════════════════
#  SESSION STATE
# ══════════════════════════════════════════════════════════════
def _init():
    today = datetime.now()
    defaults = {
        "messages": [{"role": "assistant", "content":
            "你好！請在左側設定你的**參賽角色**，然後把比賽簡章丟進來讓我評估。\n\n"
            "直接點擊行事曆上任意日期格子，即可新增或編輯任務。"}],
        "schedule":       {},
        "my_role":        "組員",
        "my_domains":     [],
        "team_size":      4,
        "weekly_checked": False,
        "gcal_service":   None,
        "calendar_id":    "primary",
        "days_passed":    0,
        "cal_year":       today.year,
        "cal_month":      today.month,
        "edit_date":      None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ══════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════
def get_day(date_str: str) -> dict:
    if date_str not in st.session_state.schedule:
        st.session_state.schedule[date_str] = {"tasks": [], "hw_wait": None, "cog_locked": False}
    return st.session_state.schedule[date_str]

def _day_dominant_load(tasks: list) -> str:
    if not tasks: return "none"
    return "high" if any(t["load"] == "high" for t in tasks) else "low"

def recompute_cog_locks(schedule: dict) -> dict:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(120):
        ds = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        if ds not in schedule:
            schedule[ds] = {"tasks": [], "hw_wait": None, "cog_locked": False}
        if i < COG_LOCK_AFTER:
            schedule[ds]["cog_locked"] = False; continue
        prev = [
            _day_dominant_load(
                schedule.get((today + timedelta(days=i-j-1)).strftime("%Y-%m-%d"), {}).get("tasks", [])
            ) for j in range(COG_LOCK_AFTER)
        ]
        schedule[ds]["cog_locked"] = all(l == "high" for l in prev)
    return schedule


# ══════════════════════════════════════════════════════════════
#  ICS EXPORT
# ══════════════════════════════════════════════════════════════
def generate_ics(schedule: dict) -> str:
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//決策小秘書 V4.6//ZH",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:決策小秘書", "X-WR-TIMEZONE:Asia/Taipei",
    ]
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for date_str, day in schedule.items():
        tasks = day.get("tasks", [])
        if not tasks: continue
        names    = list({t["name"] for t in tasks})
        notes    = "; ".join(t["notes"] for t in tasks if t.get("notes"))
        dl_list  = [t["deadline"] for t in tasks if t.get("deadline")]
        dl_str   = "截止日: " + ", ".join(dl_list) if dl_list else ""
        dt_start = date_str.replace("-", "")
        dt_end   = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
        desc     = "\\n".join(p for p in [notes, dl_str] if p) or "決策小秘書自動排入"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{date_str}-secretary@v46",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{dt_start}",
            f"DTEND;VALUE=DATE:{dt_end}",
            f"SUMMARY:🗓️ {' | '.join(names)}",
            f"DESCRIPTION:{desc}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)

# ══════════════════════════════════════════════════════════════
#  EDIT DIALOG
# ══════════════════════════════════════════════════════════════
@st.dialog("📅 編輯任務", width="large")
def edit_day_dialog(date_str: str):
    dt  = datetime.strptime(date_str, "%Y-%m-%d")
    wd  = ["一","二","三","四","五","六","日"][dt.weekday()]
    st.markdown(f"#### {dt.strftime('%Y / %m / %d')}　週{wd}")

    day   = get_day(date_str)
    tasks = day["tasks"]

    if day["cog_locked"]:
        st.warning("🔒 此日已被認知負載系統鎖定，高負載名額已滿。仍可新增低負載任務。")

    # ── 既有任務 ────────────────────────────────────────────
    to_delete = []
    for i, task in enumerate(tasks):
        with st.container(border=True):
            c1, c2, c3 = st.columns([4, 2, 1])
            with c1:
                task["name"] = st.text_input(
                    "任務名稱", value=task["name"],
                    key=f"tn_{date_str}_{i}", placeholder="輸入任務名稱")
            with c2:
                lbl = st.selectbox(
                    "負載類型", list(LOAD_OPTIONS.keys()),
                    index=list(LOAD_OPTIONS.values()).index(task["load"]),
                    key=f"tl_{date_str}_{i}")
                task["load"] = LOAD_OPTIONS[lbl]
            with c3:
                st.write(""); st.write("")
                if st.button("🗑️", key=f"td_{date_str}_{i}", help="刪除"):
                    to_delete.append(i)

            c4, c5 = st.columns([2, 3])
            with c4:
                dl_val = date.fromisoformat(task["deadline"]) if task.get("deadline") else None
                new_dl = st.date_input("截止日（選填）", value=dl_val,
                    key=f"tdl_{date_str}_{i}", format="YYYY/MM/DD")
                task["deadline"] = new_dl.isoformat() if new_dl else None
            with c5:
                task["notes"] = st.text_input(
                    "備註（選填）", value=task.get("notes",""),
                    key=f"tnotes_{date_str}_{i}", placeholder="備忘、連結…")

    for i in reversed(to_delete):
        tasks.pop(i)

    st.divider()

    # ── 新增任務 ────────────────────────────────────────────
    st.markdown("**＋ 新增任務**")
    na1, na2 = st.columns([4, 2])
    with na1:
        new_name = st.text_input("任務名稱", key=f"nn_{date_str}", placeholder="例如：韌體架構規劃")
    with na2:
        new_load_lbl = st.selectbox("負載類型", list(LOAD_OPTIONS.keys()), key=f"nl_{date_str}")
    na3, na4 = st.columns([2, 3])
    with na3:
        new_dl = st.date_input("截止日（選填）", value=None, key=f"ndl_{date_str}", format="YYYY/MM/DD")
    with na4:
        new_notes = st.text_input("備註（選填）", key=f"nn2_{date_str}", placeholder="備忘、連結…")

    if st.button("＋ 加入", type="primary", use_container_width=True):
        if new_name.strip():
            tasks.append({
                "id":       str(uuid.uuid4())[:8],
                "name":     new_name.strip(),
                "load":     LOAD_OPTIONS[new_load_lbl],
                "mine":     True,
                "deadline": new_dl.isoformat() if new_dl else None,
                "notes":    new_notes.strip(),
            })
            st.success(f"已新增「{new_name.strip()}」")
            st.rerun()
        else:
            st.warning("請輸入任務名稱")

    # 備料等待
    hw_val = day.get("hw_wait") or ""
    new_hw = st.text_input("⏳ 備料等待標記（選填）", value=hw_val,
        key=f"hw_{date_str}", placeholder="例如：備料：PCB電路設計")
    day["hw_wait"] = new_hw.strip() or None

    st.write("")
    if st.button("✅ 儲存並關閉", type="primary", use_container_width=True):
        recompute_cog_locks(st.session_state.schedule)
        st.session_state.edit_date = None
        st.rerun()

# ══════════════════════════════════════════════════════════════
#  NATIVE STREAMLIT CALENDAR GRID
# ══════════════════════════════════════════════════════════════
def build_day_label(day_num: int, ds: str, is_today: bool) -> str:
    """按鈕顯示文字：日期 + 任務清單。"""
    day_data = st.session_state.schedule.get(ds, {})
    tasks    = day_data.get("tasks", [])
    hw_wait  = day_data.get("hw_wait")
    cog_lock = day_data.get("cog_locked", False)

    today_mark = " ●" if is_today else ""
    parts = [f"{day_num}{today_mark}"]

    if cog_lock:
        parts.append("🔒 認知鎖定")
    elif hw_wait:
        parts.append(f"⏳ {hw_wait[:12]}")

    shown = {}
    for t in tasks:
        n = t["name"]
        if n not in shown:
            shown[n] = {"count": 0, "load": t["load"]}
        shown[n]["count"] += 1

    for name, info in list(shown.items())[:3]:
        emoji = "🔴" if info["load"] == "high" else "🟢"
        cnt   = f" ×{info['count']}" if info["count"] > 1 else ""
        short = name[:8] + ("…" if len(name) > 8 else "")
        parts.append(f"{emoji} {short}{cnt}")

    if len(shown) > 3:
        parts.append(f"…還有 {len(shown)-3} 項")

    return "\n".join(parts)

def get_cell_class(ds: str, is_today: bool, is_weekend: bool) -> str:
    day_data = st.session_state.schedule.get(ds, {})
    if day_data.get("cog_locked"):   return "cal-coglock"
    if day_data.get("hw_wait"):      return "cal-hwwait"
    if is_today:                     return "cal-today"
    if is_weekend:                   return "cal-weekend"
    return ""

def render_calendar(year: int, month: int):
    today_date = datetime.now().date()
    num_days   = cal_lib.monthrange(year, month)[1]
    first_wd   = datetime(year, month, 1).weekday()  # 0=Mon

    # 週標題
    header_cols = st.columns(7)
    for i, wd in enumerate(["一","二","三","四","五","六","日"]):
        with header_cols[i]:
            st.markdown(
                f"<div class='cal-header'>週{wd}</div>",
                unsafe_allow_html=True
            )

    # 建立完整 cell 陣列（0 = 空格）
    cells = [0] * first_wd + list(range(1, num_days + 1))
    while len(cells) % 7:
        cells.append(0)

    # 每週一排
    for week_start in range(0, len(cells), 7):
        week = cells[week_start: week_start + 7]
        cols = st.columns(7)
        for col_i, day_num in enumerate(week):
            with cols[col_i]:
                if day_num == 0:
                    st.markdown("<div class='cal-empty'></div>", unsafe_allow_html=True)
                else:
                    ds         = f"{year}-{month:02d}-{day_num:02d}"
                    is_today   = datetime(year, month, day_num).date() == today_date
                    is_weekend = datetime(year, month, day_num).weekday() >= 5
                    cell_cls   = get_cell_class(ds, is_today, is_weekend)
                    label      = build_day_label(day_num, ds, is_today)

                    # 包裹 div 給格子加對應的 class
                    st.markdown(f"<div class='cal-btn {cell_cls}'>", unsafe_allow_html=True)
                    if st.button(label, key=f"cal_{ds}", use_container_width=True):
                        st.session_state.edit_date = ds
                        st.rerun()
                    st.markdown("</div>", unsafe_allow_html=True)

    # 圖例
    st.markdown("""
<div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:6px;font-size:0.72em;color:#666;">
    <span>🔴 高負載</span>
    <span>🟢 低負載</span>
    <span style="color:#ffa94d">⏳ 備料等待</span>
    <span style="color:#ff6b6b">🔒 認知鎖定</span>
    <span style="color:#4dabf7">● 今天</span>
    <span>點格子可編輯</span>
</div>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
#  AI BRAIN
# ══════════════════════════════════════════════════════════════
class SecretaryBrain:

    @staticmethod
    def loop_1_parse(user_input, uploaded_files=None):
        """萬用模式：Gemini 自由估算任意輸入的子任務、工時、負載。"""
        role_ctx = (
            f"使用者角色：{st.session_state.my_role}，"
            f"負責領域：{', '.join(st.session_state.my_domains) or '未指定'}，"
            f"隊伍人數：{st.session_state.team_size} 人。"
        )
        generation_config = genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "project_name":       {"type":"STRING","description":"事項名稱，找不到填「未命名事項」"},
                    "deadline_days":      {"type":"INTEGER","description":"距截止日天數，未提及填 14"},
                    "confidence":         {"type":"STRING","description":"可以排程填 high；完全無法判斷填 low"},
                    "follow_up_question": {"type":"STRING","description":"confidence 為 low 時詢問缺少的資訊"},
                    "tasks": {
                        "type": "ARRAY",
                        "description": "拆解出的子任務，任何類型皆可（比賽、瑣事、作業、購物…）",
                        "items": {
                            "type": "OBJECT",
                            "properties": {
                                "name":               {"type":"STRING","description":"子任務名稱"},
                                "target_date":        {"type":"STRING","description":"若使用者明確指定日期（如4/8、下週三），填 YYYY-MM-DD；無特定日期填空字串"},
                                "estimated_hours":    {"type":"NUMBER","description":"預估工時（小時），最少 0.5"},
                                "load":               {"type":"STRING","description":"high=需高度專注或體力；low=輕鬆瑣碎"},
                                "is_mine":            {"type":"BOOLEAN","description":"根據使用者角色判斷，是否由本人負責"},
                                "is_hardware":        {"type":"BOOLEAN","description":"是否需要購買或等待實體零件材料"},
                                "hardware_lead_days": {"type":"INTEGER","description":"若 is_hardware=true，備料等待天數，否則填 0"},
                                "notes":              {"type":"STRING","description":"補充提醒，可空白"}
                            },
                            "required":["name","target_date","estimated_hours","load","is_mine","is_hardware","hardware_lead_days"]
                        }
                    }
                },
                "required":["project_name","deadline_days","confidence","follow_up_question","tasks"]
            },
        )
        model  = genai.GenerativeModel("gemini-2.5-flash")
        import datetime as _dt_mod
        _today_str = _dt_mod.datetime.now().strftime("%Y-%m-%d")
        system = (
            "你是萬用個人排程助理，可處理任何類型的輸入：比賽專案、日常瑣事、"
            "作業報告、購物清單、會議準備、健身計畫等，全部都要能拆解排程。\n"
            f"今天日期：{_today_str}。{role_ctx}\n"
            "【重要】target_date 欄位：若使用者明確提到特定日期（例如 4/8、明天、下週三、4月9日），"
            "必須將其轉換成 YYYY-MM-DD 格式填入 target_date。"
            "有了 target_date，系統才能把任務釘在正確的日期，而不是亂填到其他天。"
            "沒有指定日期的任務才填空字串。\n"
            "is_mine = 此子任務是否應排進使用者自己的行事曆。"
            "若使用者獨自完成或未提及分工，所有任務 is_mine 都填 true。"
        )
        parts      = [system, f"\n使用者輸入：{user_input}"]
        temp_paths = []

        if uploaded_files:
            files = uploaded_files if isinstance(uploaded_files, list) else [uploaded_files]
            for uf in files:
                ext = "." + uf.name.split(".")[-1]
                with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                    f.write(uf.getvalue()); tp = f.name
                temp_paths.append(tp)
                try:
                    parts.append(genai.upload_file(tp))
                    parts.append(f"（以上為檔案：{uf.name}）")
                except Exception as e:
                    for p in temp_paths:
                        if os.path.exists(p): os.remove(p)
                    return {"status":"error","reply":f"⚠️ 檔案「{uf.name}」上傳失敗：`{e}`"}

        try:
            resp = model.generate_content(parts, generation_config=generation_config)
            data = json.loads(resp.text)
        except Exception as e:
            data = {"confidence":"low","follow_up_question":f"API 錯誤：`{e}`",
                    "tasks":[],"deadline_days":14,"project_name":"未命名事項"}
        finally:
            for p in temp_paths:
                if os.path.exists(p): os.remove(p)

        if data.get("confidence") == "low" or not data.get("tasks"):
            return {"status":"needs_info",
                    "reply":"🛑 **資訊不足，請補充：**\n\n" + str(data.get("follow_up_question",""))}
        return {"status":"success", **data}

    @staticmethod
    def sandbox(parsed_tasks, current_schedule, deadline_days):
        today    = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deadline = min(deadline_days, 120)
        sch = {ds:{"tasks":list(v["tasks"]),"hw_wait":v["hw_wait"],"cog_locked":v["cog_locked"]}
               for ds,v in current_schedule.items()}
        recompute_cog_locks(sch)

        my_tasks    = [t for t in parsed_tasks if t.get("is_mine", True)]
        other_tasks = [t for t in parsed_tasks if not t.get("is_mine", True)]

        # ── 硬體備料標記 ──────────────────────────────────────
        hw_warnings = []
        for t in my_tasks:
            if t.get("is_hardware") and t.get("hardware_lead_days", 0) > 0:
                lead = min(t["hardware_lead_days"], deadline - 1)
                for i in range(lead):
                    ds = (today+timedelta(days=i)).strftime("%Y-%m-%d")
                    if ds not in sch: sch[ds]={"tasks":[],"hw_wait":None,"cog_locked":False}
                    if sch[ds]["hw_wait"] is None: sch[ds]["hw_wait"]=f"備料：{t['name']}"
                hw_warnings.append(f"⏳ **{t['name']}** 需備料約 {t['hardware_lead_days']} 天，前 {lead} 天標記等待期。")

        real_hours    = sum(t.get("estimated_hours", 1) for t in my_tasks)
        tasks_by_date = {}
        floating      = []   # 沒有 target_date 的任務，走「從死線往前填」

        # ── 有指定日期的任務：直接釘到那天 ─────────────────────
        for t in my_tasks:
            td = (t.get("target_date") or "").strip()
            task_entry = {
                "id":   str(uuid.uuid4())[:8],
                "name": t["name"],
                "load": t.get("load", "high"),
                "mine": True,
                "deadline": None,
                "notes": t.get("notes", ""),
            }
            if td:
                # 驗證格式，避免 Gemini 填了奇怪的字串
                try:
                    datetime.strptime(td, "%Y-%m-%d")
                    valid_date = True
                except ValueError:
                    valid_date = False

                if valid_date:
                    if td not in sch:
                        sch[td] = {"tasks":[], "hw_wait":None, "cog_locked":False}
                    # 有 target_date 的任務，忽略 DAILY_CAP 強制釘入
                    # （課表、考試、會議等固定事件必須在那天）
                    sch[td]["tasks"].append(task_entry)
                    tasks_by_date.setdefault(td, []).append(t["name"])
                    continue
            # 沒有 target_date 或格式錯誤 → 進浮動佇列
            slots = max(1, math.ceil(t.get("estimated_hours", 1)))
            floating.extend([task_entry] * slots)

        # ── 沒有指定日期的任務：從死線往前填 ───────────────────
        remaining = list(floating)
        for i in range(deadline-1, -1, -1):
            if not remaining: break
            ds = (today+timedelta(days=i)).strftime("%Y-%m-%d")
            if ds not in sch: sch[ds]={"tasks":[],"hw_wait":None,"cog_locked":False}
            avail = DAILY_CAP - len(sch[ds]["tasks"])
            if avail <= 0: continue
            placed, new_rem = 0, []
            for task in remaining:
                if placed >= avail: new_rem.append(task); continue
                if sch[ds]["cog_locked"] and task["load"]=="high": new_rem.append(task); continue
                sch[ds]["tasks"].append(task)
                tasks_by_date.setdefault(ds,[]).append(task["name"])
                placed += 1
            remaining = new_rem

        recompute_cog_locks(sch)

        # 只回報「新排入且有任務」天的認知鎖定，避免空天假警報
        newly_filled = set(tasks_by_date.keys())
        cog_lock_dates = [
            ds for ds in newly_filled
            if sch.get(ds, {}).get("cog_locked")
        ]

        return {
            "schedule":sch, "weighted_hours":real_hours, "tasks_by_date":tasks_by_date,
            "overflow":bool(remaining), "hw_warnings":hw_warnings, "cog_lock_dates":cog_lock_dates,
            "my_tasks":my_tasks, "other_tasks":other_tasks,
        }

    @staticmethod
    def loop_2_strategy(parse_data, sandbox_res):
        project     = parse_data.get("project_name","此事項")
        my_tasks    = sandbox_res["my_tasks"]
        other_tasks = sandbox_res["other_tasks"]
        w           = sandbox_res["weighted_hours"]
        deadline    = parse_data["deadline_days"]

        lines = [f"### 📋 {project}"]
        if my_tasks:
            lines.append("**你的任務：**")
            for t in my_tasks:
                h    = t.get("estimated_hours", 1)
                load = "🔴 高負載" if t.get("load") == "high" else "🟢 低負載"
                hw   = f"（需備料 {t['hardware_lead_days']} 天）" if t.get("is_hardware") else ""
                note = f"　_{t['notes']}_" if t.get("notes") else ""
                lines.append(f"- **{t['name']}**　{h}h　{load}{hw}{note}")
        if other_tasks:
            lines.append("\n**隊友 / 他人負責：**")
            for t in other_tasks:
                lines.append(f"- {t['name']}　{t.get('estimated_hours',1)}h")

        lines.append(f"\n**你的總工時估算：{w} 小時**　／　距截止 {deadline} 天")
        lines.append("---")
        for m in sandbox_res["hw_warnings"]: lines.append(m)
        if sandbox_res["cog_lock_dates"]:
            sample = ", ".join(sandbox_res["cog_lock_dates"][:3])
            lines.append(f"🧠 **認知負載警告**：已鎖定 {sample} 等日期高負載名額。")

        lines.append("")
        max_daily = DAILY_CAP * deadline
        load_pct  = int(w / max_daily * 100) if max_daily > 0 else 999

        if sandbox_res["overflow"]:
            lines.append("### 🔴 時間不夠\n死線前物理時間已擊穿。\n建議：延後截止日 / 縮減範圍 / 請人協助。")
        elif load_pct >= 80:
            lines.append(f"### 🟡 勉強可行（中等風險）\n行事曆負載達 {load_pct}%，已排入。建議盡早開始高風險項目。")
        else:
            lines.append(f"### 🟢 可以接\n行事曆負載 {load_pct}%，餘裕充足，已自動排入。")

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
        st.error("找不到 GEMINI_API_KEY，請到 Streamlit Cloud → Secrets 設定。")
        api_key = None
        st.stop()

    st.divider()

    st.header("👤 我的角色設定")
    c1, c2 = st.columns(2)
    with c1: st.selectbox("比賽角色", ["組員","隊長"], key="my_role")
    with c2: st.number_input("隊伍人數", min_value=1, max_value=10, key="team_size")
    st.multiselect("我負責的技術領域",
        ["韌體","電控","機械","AI","軟體","電路","硬體"], key="my_domains",
        help="決定哪些任務排進你的行事曆")
    if st.session_state.my_role == "隊長":
        st.caption("隊長自動負責：企劃書、系統整合，以及你選擇的領域。")

    st.divider()

    st.header("📤 匯出行事曆")
    st.caption("下載 .ics 可匯入 Apple / Samsung / Outlook 行事曆。")
    st.download_button(
        "⬇️ 下載 ICS 檔案",
        data=generate_ics(st.session_state.schedule),
        file_name="secretary_schedule.ics",
        mime="text/calendar",
        use_container_width=True,
        help="iOS：點擊後直接加入行事曆　Android：用行事曆 app 開啟",
    )

    st.divider()

    st.header("⏳ 進度防呆")
    st.slider("快轉天數（測試用）", 0, 14, key="days_passed")
    if st.session_state.days_passed >= 7 and not st.session_state.weekly_checked:
        st.error("🚨 **系統強制鎖死**\n超過 7 天未更新進度。")
        report = st.text_area("請回報本週進度以解鎖：")
        if st.button("提交解鎖"):
            if report.strip():
                st.session_state.weekly_checked = True; st.rerun()
            else:
                st.warning("請輸入實際進度後再提交。")
        st.stop()

# ══════════════════════════════════════════════════════════════
#  DIALOG TRIGGER
# ══════════════════════════════════════════════════════════════
if st.session_state.edit_date:
    edit_day_dialog(st.session_state.edit_date)

# ══════════════════════════════════════════════════════════════
#  MAIN LAYOUT
# ══════════════════════════════════════════════════════════════
col_cal, col_chat = st.columns([7, 3])

# ── 月曆（@st.fragment 讓翻頁不影響聊天）─────────────────────
@st.fragment
def calendar_section():
    yr = st.session_state.cal_year
    mo = st.session_state.cal_month

    nav_l, nav_title, nav_r = st.columns([1, 4, 1])
    with nav_l:
        if st.button("◀", use_container_width=True, help="上個月"):
            if mo == 1: st.session_state.cal_year -= 1; st.session_state.cal_month = 12
            else:       st.session_state.cal_month -= 1
            st.rerun(scope="fragment")
    with nav_title:
        st.markdown(
            f"<h3 style='text-align:center;margin:0;padding:4px 0'>🗓️ {yr} 年 {mo} 月</h3>",
            unsafe_allow_html=True)
    with nav_r:
        if st.button("▶", use_container_width=True, help="下個月"):
            if mo == 12: st.session_state.cal_year += 1; st.session_state.cal_month = 1
            else:        st.session_state.cal_month += 1
            st.rerun(scope="fragment")

    render_calendar(yr, mo)

with col_cal:
    calendar_section()

# ── 聊天 ──────────────────────────────────────────────────────
with col_chat:
    st.header("🧠 智慧推論大腦")

    with st.expander("📂 上傳簡章（PDF / PNG / JPG，可多選）", expanded=False):
        uploaded_files = st.file_uploader(
            "拖曳或選擇檔案（可同時選多個）",
            type=["pdf","png","jpg","jpeg"],
            accept_multiple_files=True,
            label_visibility="collapsed")

    chat_box = st.container(height=300)
    with chat_box:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if prompt := st.chat_input("輸入指令，例如：幫我評估這個比賽簡章"):
        if not api_key:
            st.error("API Key 未設定。"); st.stop()

        st.session_state.messages.append({"role":"user","content":prompt})
        with chat_box:
            with st.chat_message("user"): st.markdown(prompt)
            with st.chat_message("assistant"):
                with st.spinner("🔍 大腦解析中..."):
                    parsed = SecretaryBrain.loop_1_parse(prompt, uploaded_files if uploaded_files else None)

                if parsed["status"] != "success":
                    reply = parsed["reply"]
                    st.markdown(reply)
                else:
                    with st.spinner("⚙️ 沙盒模擬排程中..."):
                        sim = SecretaryBrain.sandbox(
                            parsed["tasks"], st.session_state.schedule, parsed["deadline_days"])
                    reply = SecretaryBrain.loop_2_strategy(parsed, sim)
                    st.markdown(reply)

                    if not sim["overflow"] and sim["my_tasks"]:
                        st.session_state.schedule = sim["schedule"]
                        recompute_cog_locks(st.session_state.schedule)

                st.session_state.messages.append({"role":"assistant","content":reply})
        st.rerun()
