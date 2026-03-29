"""
決策小秘書 V4.5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
部署方式（Streamlit Community Cloud）：
  1. 把此資料夾 push 到 GitHub repo
  2. 到 share.streamlit.io 連接 repo
  3. 在 App settings → Secrets 加入：
       GEMINI_API_KEY = "your-key-here"
  4. （選用）若要串接 Google Calendar：
       [google_service_account]
       type = "service_account"
       project_id = "..."
       private_key_id = "..."
       private_key = "-----BEGIN RSA PRIVATE KEY-----\n..."
       client_email = "xxx@yyy.iam.gserviceaccount.com"
       ... （完整 service-account.json 的所有欄位）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import streamlit as st
from datetime import datetime, timedelta
import google.generativeai as genai
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials as SACredentials
import json, tempfile, os, math

# ══════════════════════════════════════════════════════════════
#  PAGE CONFIG & CSS
# ══════════════════════════════════════════════════════════════
st.set_page_config(layout="wide", page_title="決策小秘書 V4.5", page_icon="🗓️")

st.markdown("""
<style>
.cal-grid {
    display: grid;
    grid-template-columns: repeat(7, 1fr);
    gap: 8px;
    margin-top: 14px;
}
.day-cell {
    border: 1px solid rgba(128,128,128,0.2);
    border-radius: 10px;
    min-height: 130px;
    padding: 10px;
    display: flex;
    flex-direction: column;
}
.day-cell.today    { border: 2px solid #4dabf7; }
.day-cell.cog-lock { border: 2px solid #ff6b6b !important;
                     background: rgba(255,107,107,0.04); }
.day-cell.hw-wait  { border: 2px dashed #ffa94d !important;
                     background: rgba(255,169,77,0.04); }
.day-num  { font-size: 1.0em; font-weight: 600; margin-bottom: 4px; }
.day-sub  { font-size: 0.65em; font-weight: 700; letter-spacing: 0.05em;
            margin-bottom: 5px; }
.sub-lock { color: #ff6b6b; }
.sub-wait { color: #ffa94d; }
.task-tag {
    font-size: 0.74em;
    padding: 2px 6px;
    border-radius: 5px;
    margin-bottom: 3px;
    display: block;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.tag-h    { background: rgba(255,107,107,0.13); color: #c92a2a; }
.tag-l    { background: rgba(81,207,102,0.13);  color: #2b8a3e; }
.tag-w    { background: rgba(255,169,77,0.13);  color: #e67700; }
.tag-mine { border-left: 3px solid #4dabf7; padding-left: 4px; }
</style>
""", unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
#  CONSTANTS
# ══════════════════════════════════════════════════════════════
SCHEDULE_DAYS  = 14
DAILY_CAP      = 4
COG_LOCK_AFTER = 3   # 連續幾天高負載後鎖定第 N+1 天
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
def _blank_schedule():
    s = []
    for i in range(SCHEDULE_DAYS):
        tasks = (
            [{"name": "既有專題", "load": "high", "mine": True} for _ in range(3)]
            if i < 5 else []
        )
        s.append({"tasks": tasks, "hw_wait": None, "cog_locked": False})
    return s

def _init():
    defaults = {
        "messages": [{"role": "assistant", "content":
            "你好！請先在左側設定 **Gemini API Key** 與你的**參賽角色**，"
            "然後把比賽簡章丟進來讓我評估。"}],
        "schedule":        _blank_schedule(),
        "my_role":         "組員",
        "my_domains":      [],
        "team_size":       4,
        "weekly_checked":  False,
        "gcal_service":    None,
        "days_passed":     0,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()

# ══════════════════════════════════════════════════════════════
#  HELPERS — COGNITIVE LOAD
# ══════════════════════════════════════════════════════════════
def _day_dominant_load(tasks: list) -> str:
    if not tasks:
        return "none"
    return "high" if any(t["load"] == "high" for t in tasks) else "low"

def recompute_cog_locks(schedule: list) -> list:
    """重新計算整個行事曆的認知鎖定狀態，回傳修改後的 schedule。"""
    for i in range(SCHEDULE_DAYS):
        if i < COG_LOCK_AFTER:
            schedule[i]["cog_locked"] = False
            continue
        prev = [_day_dominant_load(schedule[i - j - 1]["tasks"]) for j in range(COG_LOCK_AFTER)]
        schedule[i]["cog_locked"] = all(l == "high" for l in prev)
    return schedule

# ══════════════════════════════════════════════════════════════
#  HELPERS — ROLE FILTER
# ══════════════════════════════════════════════════════════════
def filter_my_tasks(all_techs: list, my_role: str, my_domains: list) -> list:
    """
    從 Gemini 解析出的全專案技術標籤中，
    篩選出「屬於這個人負責」的子集合。
    """
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

def write_to_gcal(service, tasks_by_day: dict, today: datetime, calendar_id: str = "primary"):
    results = []
    for day_idx, names in tasks_by_day.items():
        date_str = (today + timedelta(days=day_idx)).strftime("%Y-%m-%d")
        event = {
            "summary": "🗓️ " + " | ".join(set(names)),
            "description": "由決策小秘書 V4.5 自動排入",
            "start": {"date": date_str},
            "end":   {"date": date_str},
        }
        try:
            service.events().insert(calendarId=calendar_id, body=event).execute()
            results.append(f"✅ {date_str}：{', '.join(set(names))}")
        except Exception as e:
            results.append(f"⚠️ {date_str} 寫入失敗：{e}")
    return results

# ══════════════════════════════════════════════════════════════
#  AI BRAIN  (Gemini 2.5 Flash)
# ══════════════════════════════════════════════════════════════
class SecretaryBrain:

    # ── Loop 1：解析簡章 ────────────────────────────────────
    @staticmethod
    def loop_1_parse(user_input: str, uploaded_file=None) -> dict:
        generation_config = genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "OBJECT",
                "properties": {
                    "tech_tags": {
                        "type": "ARRAY",
                        "items": {"type": "STRING"},
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

        model = genai.GenerativeModel("gemini-2.5-flash")
        parts = [f"請分析以下專案需求或比賽簡章，抽取關鍵資訊：\n{user_input}"]

        temp_path = None
        if uploaded_file:
            ext = "." + uploaded_file.name.split(".")[-1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as f:
                f.write(uploaded_file.getvalue())
                temp_path = f.name
            try:
                parts.append(genai.upload_file(temp_path))
            except Exception as e:
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
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
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

        if data.get("confidence") == "low" or not data.get("tech_tags"):
            return {
                "status": "needs_info",
                "reply": f"🛑 **資訊不足，已阻斷排程。**\n\n{data.get('follow_up_question')}",
            }

        return {"status": "success", **data}

    # ── Sandbox：沙盒模擬排程 ────────────────────────────────
    @staticmethod
    def sandbox(my_techs: list, current_schedule: list, deadline: int) -> dict:
        """
        只排「我的」任務。
        新功能：
          - 硬體備料期自動標記
          - 認知負載鎖定日跳過
        """
        # 深拷貝，不污染原始 schedule
        sch = [
            {"tasks": list(d["tasks"]), "hw_wait": d["hw_wait"], "cog_locked": d["cog_locked"]}
            for d in current_schedule
        ]

        # 1. 認知鎖定（基於既有任務）
        recompute_cog_locks(sch)

        # 2. 硬體備料期標記（非阻塞，其他任務可以進這幾天）
        hw_warnings = []
        for tech in my_techs:
            meta = RAG_DB.get(tech, {})
            if meta.get("hardware") and meta.get("lead_days", 0) > 0:
                lead = min(meta["lead_days"], deadline - 1, SCHEDULE_DAYS - 1)
                for d in range(lead):
                    if sch[d]["hw_wait"] is None:
                        sch[d]["hw_wait"] = f"備料：{tech}"
                hw_warnings.append(
                    f"⏳ **{tech}** 需備料 {meta['lead_days']} 天，"
                    f"前 {lead} 天標記為非阻塞等待期。"
                )

        # 3. 計算加權工時
        total_hours = sum(RAG_DB[t]["base_hours"] for t in my_techs if t in RAG_DB)
        max_diff    = max((RAG_DB[t]["diff"] for t in my_techs if t in RAG_DB), default=1)
        weighted    = math.ceil(total_hours * (1 + (max_diff - 1) * 0.25))

        # 4. 建立任務佇列（按技術順序，每單位1小時）
        task_queue = []
        for tech in my_techs:
            meta = RAG_DB.get(tech, {})
            w = math.ceil(
                meta.get("base_hours", 4) *
                (1 + (meta.get("diff", 1) - 1) * 0.25)
            )
            load = meta.get("load", "high")
            task_queue.extend([{"name": tech, "load": load, "mine": True}] * w)

        # 5. 從死線往前填（跳過認知鎖定日的高負載名額）
        tasks_by_day: dict[int, list] = {}
        remaining = list(task_queue)

        for day in range(min(deadline, SCHEDULE_DAYS) - 1, -1, -1):
            if not remaining:
                break
            avail_slots = DAILY_CAP - len(sch[day]["tasks"])
            if avail_slots <= 0:
                continue

            placed_this_day = 0
            new_remaining = []
            for task in remaining:
                if placed_this_day >= avail_slots:
                    new_remaining.append(task)
                    continue
                # 認知鎖定日不放高負載
                if sch[day]["cog_locked"] and task["load"] == "high":
                    new_remaining.append(task)
                    continue
                sch[day]["tasks"].append(task)
                tasks_by_day.setdefault(day, []).append(task["name"])
                placed_this_day += 1

            remaining = new_remaining

        # 6. 重新計算認知鎖定（含新任務）
        recompute_cog_locks(sch)
        cog_lock_days = [i for i in range(deadline) if i < SCHEDULE_DAYS and sch[i]["cog_locked"]]

        return {
            "schedule":      sch,
            "weighted_hours": weighted,
            "tasks_by_day":  tasks_by_day,
            "overflow":      bool(remaining),
            "hw_warnings":   hw_warnings,
            "cog_lock_days": cog_lock_days,
        }

    # ── Loop 2：生成策略報告 ─────────────────────────────────
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
            f"**你的負責項目**（{len(my_techs)} 項）：{', '.join(my_techs) if my_techs else '⚠️ 無匹配，請更新左側角色設定'}",
        ]
        if not_mine:
            lines.append(f"**隊友負責**（{len(not_mine)} 項）：{', '.join(not_mine)}")
        lines.append("---")

        if not my_techs:
            lines.append("你的角色與這場比賽的技術棧沒有交集，請在左側更新「我的領域」。")
            return "\n".join(lines)

        # 硬體備料警告
        for w_msg in sandbox_res["hw_warnings"]:
            lines.append(w_msg)

        # 認知負載警告
        if sandbox_res["cog_lock_days"]:
            lock_str = ", ".join(f"Day {d+1}" for d in sandbox_res["cog_lock_days"][:4])
            lines.append(
                f"🧠 **認知負載警告**：偵測到連續高負載，"
                f"已自動鎖定 {lock_str} 的高負載名額（保留給低負載或休息）。"
            )

        lines.append("")

        # 可行性判斷
        overflow = sandbox_res["overflow"]
        max_daily = DAILY_CAP * deadline
        load_pct  = int(w / max_daily * 100) if max_daily > 0 else 999

        if overflow:
            lines.append(
                f"### 🔴 無法接案\n"
                f"加權工時 **{w} 小時**（含難度係數），死線前物理時間已擊穿。\n"
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

    # ── Gemini API（私有模式：直接讀 Secrets）─────────────
    try:
        api_key = st.secrets["GEMINI_API_KEY"]
        genai.configure(api_key=api_key)
        st.success("🧠 大腦已連線")
    except KeyError:
        st.error("找不到 GEMINI_API_KEY，請到 Streamlit Cloud → App settings → Secrets 設定。")
        api_key = None
        st.stop()

    st.divider()

    # ── 角色設定 ───────────────────────────────────────────
    st.header("👤 我的角色設定")

    c1, c2 = st.columns(2)
    with c1:
        role = st.selectbox("比賽角色", ["組員", "隊長"],
                            index=["組員","隊長"].index(st.session_state.my_role))
        st.session_state.my_role = role
    with c2:
        size = st.number_input("隊伍人數", 1, 10, int(st.session_state.team_size))
        st.session_state.team_size = size

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

    # ── Google Calendar ────────────────────────────────────
    st.header("📅 Google Calendar")

    if st.session_state.gcal_service is not None:
        st.success("✅ Calendar 已連線")
        if st.button("斷開 Calendar"):
            st.session_state.gcal_service = None
            st.rerun()
    else:
        # 優先從 Secrets 自動連線
        if "google_service_account" in st.secrets:
            try:
                sa_info = dict(st.secrets["google_service_account"])
                st.session_state.gcal_service = build_gcal_service(sa_info)
                st.success("✅ Calendar 已從 Secrets 連線")
            except Exception as e:
                st.error(f"Secrets 連線失敗：{e}")

        # 手動貼上 JSON
        with st.expander("手動連線（貼上 Service Account JSON）"):
            sa_raw = st.text_area("Service Account JSON", height=80)
            cal_id = st.text_input("Calendar ID", value="primary",
                                   help="你的 Calendar ID，通常是 email 或 primary")
            if st.button("連線"):
                try:
                    sa_info = json.loads(sa_raw)
                    st.session_state.gcal_service = build_gcal_service(sa_info)
                    st.session_state.calendar_id  = cal_id
                    st.success("連線成功！")
                    st.rerun()
                except Exception as e:
                    st.error(f"連線失敗：{e}")

        st.caption("需要建立 Google Cloud Service Account，並將 Calendar 共享給 SA 的 email。")

    st.divider()

    # ── 進度防呆 ───────────────────────────────────────────
    st.header("⏳ 進度防呆")
    days_passed = st.slider("快轉天數（測試用）", 0, 14, int(st.session_state.days_passed))
    st.session_state.days_passed = days_passed

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

# ── 行事曆 ────────────────────────────────────────────────────
with col_cal:
    st.header("🗓️ 我的專案行事曆（14 天）")

    today = datetime.now()
    cal_html = '<div class="cal-grid">'

    for i in range(SCHEDULE_DAYS):
        d      = st.session_state.schedule[i]
        dt     = today + timedelta(days=i)
        date_s = dt.strftime("%m/%d").lstrip("0")   # e.g. "3/29"
        wd     = ["一","二","三","四","五","六","日"][dt.weekday()]

        cell_cls = "day-cell" + (" today" if i == 0 else "") + \
                   (" cog-lock" if d["cog_locked"] else "") + \
                   (" hw-wait"  if d["hw_wait"] and not d["cog_locked"] else "")

        # 小標籤
        sub_html = ""
        if d["cog_locked"]:
            sub_html = '<div class="day-sub sub-lock">🔒 認知鎖定</div>'
        elif d["hw_wait"]:
            sub_html = f'<div class="day-sub sub-wait">⏳ {d["hw_wait"]}</div>'

        # 任務標籤（相同名稱合併顯示 ×N）
        shown: dict[str, dict] = {}
        for t in d["tasks"]:
            name = t["name"]
            if name not in shown:
                shown[name] = {"count": 0, "load": t["load"], "mine": t.get("mine", False)}
            shown[name]["count"] += 1

        tags_html = ""
        for name, info in shown.items():
            lc  = "tag-h" if info["load"] == "high" else ("tag-l" if info["load"] == "low" else "tag-w")
            mc  = " tag-mine" if info["mine"] else ""
            lbl = name + (f" ×{info['count']}" if info["count"] > 1 else "")
            tags_html += f'<span class="task-tag {lc}{mc}">{lbl}</span>'

        cal_html += f'''
        <div class="{cell_cls}">
            <div class="day-num">{date_s} 週{wd}</div>
            {sub_html}
            {tags_html}
        </div>'''

    cal_html += "</div>"
    st.markdown(cal_html, unsafe_allow_html=True)

    # 圖例
    st.markdown("""
    <div style="margin-top:10px; font-size:0.78em; opacity:0.65; display:flex; gap:16px; flex-wrap:wrap;">
        <span style="color:#c92a2a">■ 高負載</span>
        <span style="color:#2b8a3e">■ 低負載</span>
        <span style="color:#e67700">■ 備料等待</span>
        <span style="border-left:3px solid #4dabf7; padding-left:4px;">藍邊 = 我的任務</span>
        <span style="color:#ff6b6b">🔒 = 認知鎖定日</span>
    </div>
    """, unsafe_allow_html=True)

# ── 聊天 ──────────────────────────────────────────────────────
with col_chat:
    st.header("🧠 智慧推論大腦")

    uploaded_file = st.file_uploader(
        "📂 上傳簡章（PDF / PNG / JPG）",
        type=["pdf", "png", "jpg", "jpeg"]
    )

    chat_box = st.container(height=480)
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

                # ─ Loop 1：解析 ─────────────────────────
                with st.spinner("🔍 大腦解析中..."):
                    parsed = SecretaryBrain.loop_1_parse(prompt, uploaded_file)

                if parsed["status"] != "success":
                    reply = parsed["reply"]
                    st.markdown(reply)
                else:
                    # ─ 角色過濾 ─────────────────────────
                    my_techs = filter_my_tasks(
                        parsed["tech_tags"],
                        st.session_state.my_role,
                        st.session_state.my_domains,
                    )

                    # ─ 沙盒模擬 ─────────────────────────
                    deadline = min(parsed["deadline_days"], SCHEDULE_DAYS)
                    with st.spinner("⚙️ 沙盒模擬排程中..."):
                        sim = SecretaryBrain.sandbox(
                            my_techs,
                            st.session_state.schedule,
                            deadline,
                        )

                    # ─ Loop 2：策略報告 ──────────────────
                    reply = SecretaryBrain.loop_2_strategy(parsed, my_techs, sim)
                    st.markdown(reply)

                    # ─ 更新行事曆（可行才寫入）─────────
                    if not sim["overflow"] and my_techs:
                        st.session_state.schedule = sim["schedule"]
                        recompute_cog_locks(st.session_state.schedule)

                        # ─ Google Calendar 寫入 ──────────
                        gcal_svc = st.session_state.gcal_service
                        if gcal_svc and sim["tasks_by_day"]:
                            cal_id = st.session_state.get("calendar_id", "primary")
                            with st.spinner("📅 寫入 Google Calendar..."):
                                results = write_to_gcal(gcal_svc, sim["tasks_by_day"], today, cal_id)
                            with st.expander("📅 Calendar 寫入結果"):
                                for r in results:
                                    st.markdown(r)
                        elif not gcal_svc:
                            st.info("💡 連接 Google Calendar 可自動同步排程。")

                st.session_state.messages.append({"role": "assistant", "content": reply})

        st.rerun()