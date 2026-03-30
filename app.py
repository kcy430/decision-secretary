"""
決策小秘書 V4.5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
部署（Streamlit Community Cloud）：
  1. Push 整個資料夾（含 .streamlit/config.toml）到 GitHub
  2. share.streamlit.io 連接 repo，主檔選 app.py
  3. App settings → Secrets 加入：
       GEMINI_API_KEY = "AIza..."
  4. （選用）Google Calendar：
       [google_service_account]
       type = "service_account"
       ... （完整 service-account.json 內容）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime, timedelta
import calendar as cal_lib
import google.generativeai as genai
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials as SACredentials
import json, tempfile, os, math

# ══════════════════════════════════════════════════════════════
#  PAGE CONFIG
# ══════════════════════════════════════════════════════════════
st.set_page_config(layout="wide", page_title="決策小秘書 V4.5", page_icon="🗓️")

st.markdown("""
<style>
    section[data-testid="stSidebar"] { background-color: #252525; }
    .block-container { padding-top: 1.5rem; }
    header[data-testid="stHeader"] { background-color: #1e1e1e; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════
DAILY_CAP      = 4
COG_LOCK_AFTER = 3
GCAL_SCOPES    = ["https://www.googleapis.com/auth/calendar.events"]

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
def _blank_schedule() -> dict:
    """排程改用 {date_str: day_data} 字典，支援無限跨月。"""
    s: dict = {}
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    count = 0
    d = today
    while count < 5:
        if d.weekday() < 5:
            ds = d.strftime("%Y-%m-%d")
            s[ds] = {
                "tasks":      [{"name": "既有專題", "load": "high", "mine": True}] * 3,
                "hw_wait":    None,
                "cog_locked": False,
            }
            count += 1
        d += timedelta(days=1)
    return s

def _init():
    today = datetime.now()
    defaults = {
        "messages": [{"role": "assistant", "content":
            "你好！請先在左側確認 **Gemini API Key** 已連線，並設定你的**參賽角色**，"
            "然後把比賽簡章丟進來讓我評估。"}],
        "schedule":       _blank_schedule(),
        "my_role":        "組員",
        "my_domains":     [],
        "team_size":      4,
        "weekly_checked": False,
        "gcal_service":   None,
        "calendar_id":    "primary",
        "days_passed":    0,
        "cal_year":       today.year,
        "cal_month":      today.month,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ══════════════════════════════════════════════════════════════
#  HELPERS — 認知負載
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
        prev = []
        for j in range(COG_LOCK_AFTER):
            pd = (today + timedelta(days=i - j - 1)).strftime("%Y-%m-%d")
            prev.append(_day_dominant_load(schedule.get(pd, {}).get("tasks", [])))
        schedule[ds]["cog_locked"] = all(l == "high" for l in prev)
    return schedule

# ══════════════════════════════════════════════════════════════
#  HELPERS — 角色過濾
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
#  GOOGLE CALENDAR
# ══════════════════════════════════════════════════════════════
def build_gcal_service(sa_info: dict):
    creds = SACredentials.from_service_account_info(sa_info, scopes=GCAL_SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def write_to_gcal(service, tasks_by_date: dict, calendar_id: str = "primary") -> list:
    results = []
    for date_str, names in tasks_by_date.items():
        event = {
            "summary":     "🗓️ " + " | ".join(set(names)),
            "description": "由決策小秘書 V4.5 自動排入",
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
#  CALENDAR RENDERER（月曆格式，深色主題）
# ══════════════════════════════════════════════════════════════
def render_month_html(schedule: dict, year: int, month: int) -> str:
    today_date = datetime.now().date()
    num_days   = cal_lib.monthrange(year, month)[1]
    first_wd   = datetime(year, month, 1).weekday()  # 0=Mon

    css = """
<style>
* { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 0; }
body { background: #1e1e1e; color: #e0e0e0; }
.cal-wrap { padding: 4px 2px; }
.cal-grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 5px;
}
.day-header {
    text-align: center;
    font-size: 0.72em;
    font-weight: 600;
    color: #666;
    padding: 4px 0 6px;
    letter-spacing: 0.05em;
}
.day-cell {
    background: #2a2a2a;
    border: 1px solid #363636;
    border-radius: 8px;
    min-height: 72px;
    padding: 5px;
    display: flex;
    flex-direction: column;
}
.day-cell.empty   { background: transparent; border: none; min-height: 72px; }
.day-cell.today   { border: 1.5px solid #4dabf7; }
.day-cell.weekend { background: #252525; }
.day-cell.cog-lock { border: 1.5px solid #ff6b6b; background: #2e2020; }
.day-cell.hw-wait  { border: 1.5px dashed #ffa94d; background: #2c2618; }
.day-num { font-size: 0.78em; font-weight: 600; color: #bbb; margin-bottom: 3px; }
.day-num.is-today { color: #4dabf7; }
.day-num.is-weekend { color: #888; }
.day-sub {
    font-size: 0.60em; font-weight: 700; letter-spacing: 0.04em;
    margin-bottom: 3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sub-lock { color: #ff6b6b; }
.sub-wait { color: #ffa94d; }
.task-tag {
    font-size: 0.68em; padding: 1px 5px; border-radius: 4px;
    margin-bottom: 2px; display: block;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.tag-h    { background: rgba(255,107,107,0.18); color: #ff8787; }
.tag-l    { background: rgba(81,207,102,0.18);  color: #69db7c; }
.tag-w    { background: rgba(255,169,77,0.18);  color: #ffc078; }
.tag-mine { border-left: 2px solid #4dabf7; padding-left: 4px; }
.legend {
    display: flex; gap: 14px; flex-wrap: wrap;
    margin-top: 8px; padding: 0 2px;
    font-size: 0.68em; color: #666;
}
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
                shown[n] = {"count": 0, "load": t["load"], "mine": t.get("mine", False)}
            shown[n]["count"] += 1

        tags_html = ""
        for name, info in shown.items():
            lc  = "tag-h" if info["load"] == "high" else ("tag-l" if info["load"] == "low" else "tag-w")
            mc  = " tag-mine" if info["mine"] else ""
            lbl = name + (f" ×{info['count']}" if info["count"] > 1 else "")
            tags_html += f'<span class="task-tag {lc}{mc}">{lbl}</span>'

        html += f'<div class="{cell_cls}"><div class="{num_cls}">{day}</div>{sub_html}{tags_html}</div>'

    html += "</div>"  # cal-grid
    html += """
<div class="legend">
    <span style="color:#ff8787">■ 高負載</span>
    <span style="color:#69db7c">■ 低負載</span>
    <span style="color:#ffc078">■ 備料等待</span>
    <span style="border-left:2px solid #4dabf7;padding-left:4px;color:#4dabf7;">藍邊 = 我的任務</span>
    <span style="color:#ff6b6b">🔒 認知鎖定</span>
    <span style="color:#4dabf7">● 今天</span>
</div>
</div>"""

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
                    "tech_tags": {
                        "type": "ARRAY", "items": {"type": "STRING"},
                        "description": f"抽取技術標籤，只能從以下挑選：{TECH_TAGS}",
                    },
                    "confidence": {
                        "type": "STRING",
                        "description": "資訊充足填 high；技術不明或無死線填 low",
                    },
                    "follow_up_question": {
                        "type": "STRING",
                        "description": "若 confidence 為 low，提出問題詢問缺少的資訊",
                    },
                    "deadline_days": {
                        "type": "INTEGER",
                        "description": "距死線天數，未提及預設填 14",
                    },
                    "project_name": {
                        "type": "STRING",
                        "description": "專案或比賽名稱，找不到填「未命名專案」",
                    },
                },
                "required": ["tech_tags", "confidence", "follow_up_question",
                             "deadline_days", "project_name"],
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
            data = {
                "confidence": "low",
                "follow_up_question": f"API 底層錯誤：`{e}`",
                "tech_tags": [], "deadline_days": 14, "project_name": "未命名專案",
            }
        finally:
            if temp_path and os.path.exists(temp_path): os.remove(temp_path)

        if data.get("confidence") == "low" or not data.get("tech_tags"):
            return {
                "status": "needs_info",
                "reply": f"🛑 **資訊不足，已阻斷排程。**\n\n{data.get('follow_up_question')}",
            }
        return {"status": "success", **data}

    @staticmethod
    def sandbox(my_techs: list, current_schedule: dict, deadline_days: int) -> dict:
        today    = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deadline = min(deadline_days, 120)

        sch: dict = {
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
                    f"⏳ **{tech}** 需備料 {meta['lead_days']} 天，前 {lead} 天標記為等待期。"
                )

        total_hours = sum(RAG_DB[t]["base_hours"] for t in my_techs if t in RAG_DB)
        max_diff    = max((RAG_DB[t]["diff"] for t in my_techs if t in RAG_DB), default=1)
        weighted    = math.ceil(total_hours * (1 + (max_diff - 1) * 0.25))

        task_queue = []
        for tech in my_techs:
            meta = RAG_DB.get(tech, {})
            w    = math.ceil(meta.get("base_hours", 4) * (1 + (meta.get("diff", 1) - 1) * 0.25))
            task_queue.extend([{"name": tech, "load": meta.get("load", "high"), "mine": True}] * w)

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
                if placed >= avail:
                    new_rem.append(task); continue
                if sch[ds]["cog_locked"] and task["load"] == "high":
                    new_rem.append(task); continue
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
            "schedule":       sch,
            "weighted_hours": weighted,
            "tasks_by_date":  tasks_by_date,
            "overflow":       bool(remaining),
            "hw_warnings":    hw_warnings,
            "cog_lock_dates": cog_lock_dates,
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
            lines.append(
                f"🧠 **認知負載警告**：偵測到連續高負載，"
                f"已鎖定 {sample} 等日期的高負載名額。"
            )

        lines.append("")
        max_daily = DAILY_CAP * deadline
        load_pct  = int(w / max_daily * 100) if max_daily > 0 else 999

        if sandbox_res["overflow"]:
            lines.append(
                f"### 🔴 無法接案\n"
                f"加權工時 **{w} 小時**，死線前物理時間已擊穿。\n"
                f"建議：延後死線 / 縮減功能 / 增加人手。"
            )
        elif load_pct >= 80:
            lines.append(
                f"### 🟡 勉強可接（中等風險）\n"
                f"加權工時 **{w} 小時**，行事曆負載達 {load_pct}%，已排入。\n"
                f"建議：本週末先完成環境架設，把風險前壓。"
            )
        else:
            lines.append(
                f"### 🟢 適合接案\n"
                f"加權工時 **{w} 小時**，行事曆負載 {load_pct}%，餘裕充足，已自動排入。"
            )

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
        role = st.selectbox("比賽角色", ["組員", "隊長"],
                            index=["組員","隊長"].index(st.session_state.my_role))
        st.session_state.my_role = role
    with c2:
        st.number_input("隊伍人數", min_value=1, max_value=10, key="team_size")

    domains = st.multiselect(
        "我負責的技術領域",
        ["韌體", "電控", "機械", "AI", "軟體", "電路", "硬體"],
        default=st.session_state.my_domains,
        help="決定哪些任務會排進你的行事曆",
    )
    st.session_state.my_domains = domains
    if role == "隊長":
        st.caption("隊長自動負責：企劃書、系統整合，以及你選擇的領域。")

    st.divider()

    st.header("📅 Google Calendar")
    if st.session_state.gcal_service is not None:
        st.success("✅ Calendar 已連線")
        if st.button("斷開 Calendar"):
            st.session_state.gcal_service = None
            st.rerun()
    else:
        if "google_service_account" in st.secrets:
            try:
                sa_info = dict(st.secrets["google_service_account"])
                st.session_state.gcal_service = build_gcal_service(sa_info)
                st.success("✅ Calendar 已從 Secrets 連線")
            except Exception as e:
                st.error(f"Secrets 連線失敗：{e}")

        with st.expander("手動連線（貼上 Service Account JSON）"):
            sa_raw = st.text_area("Service Account JSON", height=80)
            cal_id = st.text_input("Calendar ID", value="primary")
            if st.button("連線"):
                try:
                    sa_info = json.loads(sa_raw)
                    st.session_state.gcal_service = build_gcal_service(sa_info)
                    st.session_state.calendar_id  = cal_id
                    st.success("連線成功！")
                    st.rerun()
                except Exception as e:
                    st.error(f"連線失敗：{e}")

    st.divider()

    st.header("⏳ 進度防呆")
    st.slider("快轉天數（測試用）", 0, 14, key="days_passed")
    days_passed = st.session_state.days_passed

    if days_passed >= 7 and not st.session_state.weekly_checked:
        st.error("🚨 **系統強制鎖死**\n超過 7 天未更新進度，已暫停所有功能。")
        report = st.text_area("請回報本週進度以解鎖：")
        if st.button("提交解鎖"):
            if report.strip():
                st.session_state.weekly_checked = True
                st.rerun()
            else:
                st.warning("請輸入實際進度後再提交。")
        st.stop()

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
            if mo == 1:
                st.session_state.cal_year  -= 1
                st.session_state.cal_month  = 12
            else:
                st.session_state.cal_month -= 1
            st.rerun()
    with nav_title:
        st.markdown(
            f"<h3 style='text-align:center;margin:0;padding:4px 0'>🗓️ {yr} 年 {mo} 月</h3>",
            unsafe_allow_html=True,
        )
    with nav_r:
        if st.button("▶", use_container_width=True, help="下個月"):
            if mo == 12:
                st.session_state.cal_year  += 1
                st.session_state.cal_month  = 1
            else:
                st.session_state.cal_month += 1
            st.rerun()

    num_days  = cal_lib.monthrange(yr, mo)[1]
    first_wd  = datetime(yr, mo, 1).weekday()
    num_rows  = math.ceil((first_wd + num_days) / 7)
    cal_height = num_rows * 82 + 100   # 每列 82px + 週標題與圖例緩衝

    components.html(
        render_month_html(st.session_state.schedule, yr, mo),
        height=cal_height,
        scrolling=False,
    )

# ── 聊天 ──────────────────────────────────────────────────────
with col_chat:
    st.header("🧠 智慧推論大腦")

    uploaded_file = st.file_uploader(
        "📂 上傳簡章（PDF / PNG / JPG）",
        type=["pdf", "png", "jpg", "jpeg"]
    )

    chat_box = st.container(height=380)
    with chat_box:
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if prompt := st.chat_input("輸入指令，例如：幫我評估這個比賽簡章"):
        if not api_key:
            st.error("API Key 未設定，請檢查 Streamlit Cloud Secrets。")
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
                    deadline = parsed["deadline_days"]

                    with st.spinner("⚙️ 沙盒模擬排程中..."):
                        sim = SecretaryBrain.sandbox(
                            my_techs, st.session_state.schedule, deadline
                        )

                    reply = SecretaryBrain.loop_2_strategy(parsed, my_techs, sim)
                    st.markdown(reply)

                    if not sim["overflow"] and my_techs:
                        st.session_state.schedule = sim["schedule"]
                        recompute_cog_locks(st.session_state.schedule)

                        gcal_svc = st.session_state.gcal_service
                        if gcal_svc and sim["tasks_by_date"]:
                            cal_id = st.session_state.get("calendar_id", "primary")
                            with st.spinner("📅 寫入 Google Calendar..."):
                                results = write_to_gcal(gcal_svc, sim["tasks_by_date"], cal_id)
                            with st.expander("📅 Calendar 寫入結果"):
                                for r in results: st.markdown(r)
                        elif not gcal_svc:
                            st.info("💡 連接 Google Calendar 可自動同步排程。")

                st.session_state.messages.append({"role": "assistant", "content": reply})

        st.rerun()
