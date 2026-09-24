import streamlit as st
import pandas as pd
import re
import json
import ast
import os
import glob
import requests
from datetime import datetime, timedelta
import plotly.graph_objects as go
from plotly.subplots import make_subplots
session = requests.Session()
session.trust_env = False  # 忽略環境變數（包含 proxy）

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_ICON_PATH = os.path.join(_BASE_DIR, "assets", "APX_Agent_icon.png")

st.set_page_config(
    page_title="APX Agent Dashboard",
    page_icon=_PAGE_ICON_PATH if os.path.exists(_PAGE_ICON_PATH) else None,
    layout="wide",
)

EE_AGENT_API_BASE_URL = "http://127.0.0.1:8099"
PE_AGENT_API_BASE_URL = "http://127.0.0.1:8088"
QA_AGENT_API_BASE_URL = "http://127.0.0.1:8077"
EXEC_AGENT_API_BASE_URL = "http://127.0.0.1:8110"

EE_AGENT_CHAT_URL = "http://10.11.33.250:8504"
PE_AGENT_CHAT_URL = "http://10.11.33.250:8505"
QA_AGENT_CHAT_URL = "http://10.11.33.250:8506"
KPI_API_BASE_URL = "http://10.11.33.250:8005"


# ── 暫緩決策持久化 ──────────────────────────────────────────────────
_DEFERRED_DECISIONS_FILE = os.path.join(_BASE_DIR, "deferred_decisions.json")
_WEEKLY_DATA_CACHE_FILE = os.path.join(_BASE_DIR, "weekly_data_cache.json")
_ANALYSIS_RESULTS_FILE = os.path.join(_BASE_DIR, "analysis_results_cache.json")
_CHAT_FEEDBACK_FILE = os.path.join(_BASE_DIR, "chat_feedback.json")

def _current_iso_week_key() -> str:
    iso = datetime.now().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _parse_week_num(week_value) -> int | None:
    """將週次欄位轉為週數（支援 W20、20、2026-W20）。"""
    if week_value is None:
        return None
    text = str(week_value).strip()
    if not text:
        return None

    m = re.search(r"W(\d{1,2})", text, re.IGNORECASE)
    if m:
        try:
            wk = int(m.group(1))
            return wk if 1 <= wk <= 53 else None
        except ValueError:
            return None

    if text.isdigit():
        wk = int(text)
        return wk if 1 <= wk <= 53 else None
    return None


def _normalize_week_label(week_value) -> str | None:
    """將任意週次格式正規化為 W01~W53。"""
    wk = _parse_week_num(week_value)
    return f"W{wk:02d}" if wk is not None else None


def _to_year_week(week_label: str) -> str:
    if isinstance(week_label, str) and week_label.startswith("W"):
        current_year = datetime.now().isocalendar().year
        return f"{current_year}-{week_label}"
    return ""


def _latest_week_num_from_records(records: list[dict]) -> int | None:
    week_nums = []
    for r in records:
        if not isinstance(r, dict):
            continue
        wk = _parse_week_num(r.get("Week"))
        if wk is not None:
            week_nums.append(wk)
    return max(week_nums) if week_nums else None


def _load_weekly_data_cache() -> dict:
    if not os.path.exists(_WEEKLY_DATA_CACHE_FILE):
        return {}
    try:
        with open(_WEEKLY_DATA_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _to_json_safe(value):
    """將快取資料遞迴轉為可 JSON 序列化的型別。"""
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if callable(value):
        return str(value)

    # pandas / numpy scalar（含 datetime64、int64、float64）轉為原生 python 型別
    if hasattr(value, "item"):
        try:
            return _to_json_safe(value.item())
        except Exception:
            pass

    # 將 NA/NaN 轉為 None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass

    return value


def _save_weekly_data_cache(cache: dict) -> None:
    safe_cache = _to_json_safe(cache)
    with open(_WEEKLY_DATA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(safe_cache, f, ensure_ascii=False, indent=2)


def _load_analysis_results_cache() -> dict:
    if not os.path.exists(_ANALYSIS_RESULTS_FILE):
        return {}
    try:
        with open(_ANALYSIS_RESULTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_analysis_results_cache(cache: dict) -> None:
    with open(_ANALYSIS_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(_to_json_safe(cache), f, ensure_ascii=False, indent=2)


def _build_yield_signature(yield_data: dict | None) -> str:
    payload = _to_json_safe(yield_data or {})
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _get_cached_week_analysis(week_label: str) -> dict:
    cache = _load_analysis_results_cache()
    by_week = cache.get("by_week", {})
    record = by_week.get(week_label, {})
    return record if isinstance(record, dict) else {}


def _upsert_cached_week_analysis(week_label: str, updates: dict) -> dict:
    cache = _load_analysis_results_cache()
    by_week = cache.setdefault("by_week", {})
    current = by_week.get(week_label, {})
    if not isinstance(current, dict):
        current = {}
    current.update(_to_json_safe(updates))
    current["updated_at"] = datetime.now().isoformat()
    by_week[week_label] = current
    _save_analysis_results_cache(cache)
    return current


def _load_or_build_week_analysis_bundle(
    week_label: str,
    yield_data: dict | None = None,
    force_refresh: bool = False,
) -> tuple[dict, bool]:
    """回傳週分析結果；若已有快取則直接使用，避免刷新後內容消失。"""
    cached = _get_cached_week_analysis(week_label)
    required_keys = [
        "ee_report", "ee_trace", "pe_report", "pe_trace",
        "qa_report", "qa_trace", "exec_report", "exec_trace",
    ]
    yield_signature = _build_yield_signature(yield_data)
    has_full_cache = all(k in cached for k in required_keys)
    same_yield_signature = cached.get("yield_signature") == yield_signature

    if (not force_refresh) and has_full_cache and same_yield_signature:
        return cached, True

    # 右上 Rerun 需強制走 API，先清掉 @st.cache_data 的函式快取。
    if force_refresh:
        load_ee_latest_week_payload.clear()
        load_pe_latest_week_payload.clear()
        load_qa_latest_week_payload.clear()
        load_executive_decision_payload.clear()

    ee_payload = load_ee_latest_week_payload(week_label)
    pe_payload = load_pe_latest_week_payload(week_label, yield_data=yield_data)
    qa_payload = load_qa_latest_week_payload(week_label)
    exec_payload = load_executive_decision_payload(
        week_label,
        ee_report=str(ee_payload.get("output") or ""),
        qa_report=str(qa_payload.get("output") or ""),
        pe_report=str(pe_payload.get("output") or ""),
    )

    updated = _upsert_cached_week_analysis(
        week_label,
        {
            "yield_signature": yield_signature,
            "yield_data": _to_json_safe(yield_data or {}),
            "ee_report": str(ee_payload.get("output") or "EE Agent 無回傳內容"),
            "ee_trace": ee_payload.get("trace") if isinstance(ee_payload.get("trace"), dict) else {},
            "pe_report": str(pe_payload.get("output") or "PE Agent 無回傳內容"),
            "pe_trace": pe_payload.get("trace") if isinstance(pe_payload.get("trace"), dict) else {},
            "qa_report": str(qa_payload.get("output") or "QA Agent 無回傳內容"),
            "qa_trace": qa_payload.get("trace") if isinstance(qa_payload.get("trace"), dict) else {},
            "exec_report": str(exec_payload.get("output") or "Executive Agent 無回傳內容"),
            "exec_trace": exec_payload.get("trace") if isinstance(exec_payload.get("trace"), dict) else {},
        },
    )
    return updated, False


def _load_cached_week_decisions(week_label: str) -> dict:
    cached = _get_cached_week_analysis(week_label)
    decisions = cached.get("exec_decisions", {})
    return decisions if isinstance(decisions, dict) else {}


def _save_cached_week_decisions(week_label: str, decisions: dict) -> None:
    _upsert_cached_week_analysis(week_label, {"exec_decisions": _to_json_safe(decisions)})


def _load_cached_week_actions(week_label: str) -> dict:
    cached = _get_cached_week_analysis(week_label)
    actions = cached.get("exec_actions", {})
    return actions if isinstance(actions, dict) else {}


def _save_cached_week_actions(week_label: str, actions: dict) -> None:
    _upsert_cached_week_analysis(week_label, {"exec_actions": _to_json_safe(actions)})


def _week_decisions_state_key(week_label: str) -> str:
    return f"exec_decisions::{week_label}"


def _week_actions_state_key(week_label: str) -> str:
    return f"exec_actions::{week_label}"


def _load_week_review_state(week_label: str) -> tuple[dict, dict]:
    decisions_key = _week_decisions_state_key(week_label)
    actions_key = _week_actions_state_key(week_label)

    if decisions_key not in st.session_state:
        st.session_state[decisions_key] = _load_cached_week_decisions(week_label)
    if actions_key not in st.session_state:
        st.session_state[actions_key] = _load_cached_week_actions(week_label)

    decisions = st.session_state.get(decisions_key, {})
    actions = st.session_state.get(actions_key, {})
    if not isinstance(decisions, dict):
        decisions = {}
        st.session_state[decisions_key] = decisions
    if not isinstance(actions, dict):
        actions = {}
        st.session_state[actions_key] = actions
    return decisions, actions


def _update_week_decision_status(week_label: str, dkey: str, status: str) -> None:
    decisions, _ = _load_week_review_state(week_label)
    decisions[dkey] = status
    _save_cached_week_decisions(week_label, decisions)


def _save_week_action_item(week_label: str, dkey: str, action_item: dict) -> None:
    _, actions = _load_week_review_state(week_label)
    actions[dkey] = _to_json_safe(action_item)
    _save_cached_week_actions(week_label, actions)


def _get_weekly_snapshot(data_key: str, week_key: str, builder) -> pd.DataFrame:
    """同一 ISO 週內固定資料，跨週才重新產生。"""
    cache = _load_weekly_data_cache()
    record = cache.get(data_key, {})
    cached_week = record.get("week_key")
    cached_data = record.get("data")

    if cached_week == week_key and isinstance(cached_data, list):
        # 同週內若來源已補上新週次（如 W19 -> W20），仍要允許刷新。
        current_week_num = datetime.now().isocalendar().week
        cached_latest_week = _latest_week_num_from_records(cached_data)
        if cached_latest_week is not None and cached_latest_week >= current_week_num:
            return pd.DataFrame(cached_data)

    fresh_df = builder()

    # 來源暫時異常時，沿用上一次有效快照避免整頁變空。
    if not isinstance(fresh_df, pd.DataFrame) or fresh_df.empty:
        if isinstance(cached_data, list) and cached_data:
            return pd.DataFrame(cached_data)
        return pd.DataFrame()

    cache[data_key] = {
        "week_key": week_key,
        "updated_at": datetime.now().isoformat(),
        "data": _to_json_safe(fresh_df.to_dict(orient="records")),
    }
    _save_weekly_data_cache(cache)
    return fresh_df


def _load_deferred_history() -> dict:
    """讀取歷史暫緩決策紀錄。格式：{machine: [{week, priority, instruction, deferred_at}, ...]}"""
    if not os.path.exists(_DEFERRED_DECISIONS_FILE):
        return {}
    try:
        with open(_DEFERRED_DECISIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_deferred_history(history: dict) -> None:
    """寫入暫緩決策紀錄。"""
    with open(_DEFERRED_DECISIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _record_deferred_decision(week_label: str, item: dict) -> None:
    """記錄一筆暫緩決策到歷史檔案。"""
    history = _load_deferred_history()
    machine = item.get("machine", "")
    if not machine:
        return
    records = history.setdefault(machine, [])
    # 避免同一週重複記錄
    if any(r["week"] == week_label for r in records):
        return
    records.append({
        "week": week_label,
        "priority": item.get("priority", ""),
        "instruction": item.get("instruction", ""),
        "deferred_at": datetime.now().isoformat(),
    })
    _save_deferred_history(history)


def _remove_deferred_decision(week_label: str, machine: str) -> None:
    """當決策從暫緩改為核准時，移除該週的暫緩紀錄。"""
    history = _load_deferred_history()
    if machine not in history:
        return
    history[machine] = [r for r in history[machine] if r["week"] != week_label]
    if not history[machine]:
        del history[machine]
    _save_deferred_history(history)


def _get_previous_deferrals(current_week: str, machine: str) -> list:
    """取得該機台在過去週次的暫緩紀錄（不含本週）。"""
    history = _load_deferred_history()
    records = history.get(machine, [])
    return [r for r in records if r["week"] != current_week]


def _collect_decision_status_rows(decision_items: list, week_label: str = "") -> list:
    rows = []
    decisions, actions = _load_week_review_state(week_label)
    for item in decision_items:
        dkey = f"{item.get('priority', '')}_{item.get('machine', '')}"
        status = decisions.get(dkey, "pending")
        action = actions.get(dkey, {}) if isinstance(actions.get(dkey, {}), dict) else {}
        rows.append(
            {
                "priority": item.get("priority", ""),
                "machine": item.get("machine", ""),
                "status": status,
                "lead_dept": item.get("lead_dept", ""),
                "collab_dept": item.get("collab_dept", ""),
                "deadline": item.get("deadline", ""),
                "instruction": item.get("instruction", ""),
                "reason": item.get("reason", ""),
                "expected": item.get("expected", ""),
                "action_status": str(action.get("action_status") or ""),
                "action_owner": str(action.get("owner") or ""),
                "action_notes": str(action.get("notes") or ""),
                "action_start_time": str(action.get("start_time") or ""),
                "action_end_time": str(action.get("end_time") or ""),
                "action_updated_at": str(action.get("updated_at") or ""),
            }
        )
    return rows

# print(session.get(PE_AGENT_API_BASE_URL))


def _render_action_completion_block(
    decision_items: list,
    week_decisions: dict,
    week_actions: dict,
) -> None:
    """渲染每週 Action 完成度摘要區塊（核准率 + 處置完成率 + 狀態卡片 + 進度條）。"""
    total = len(decision_items)
    if total == 0:
        return

    approved_cnt = sum(1 for v in week_decisions.values() if v == "approved")
    on_hold_cnt = sum(1 for v in week_decisions.values() if v == "on_hold")
    pending_cnt = total - approved_cnt - on_hold_cnt

    done_cnt = in_progress_cnt = blocked_cnt = not_started_cnt = 0
    for item in decision_items:
        dkey = f"{item['priority']}_{item['machine']}"
        if week_decisions.get(dkey, "pending") != "approved":
            continue
        action = week_actions.get(dkey, {})
        if not isinstance(action, dict):
            action = {}
        a_status = str(action.get("action_status") or "未開始")
        if a_status == "已完成":
            done_cnt += 1
        elif a_status == "進行中":
            in_progress_cnt += 1
        elif a_status == "阻塞":
            blocked_cnt += 1
        else:
            not_started_cnt += 1

    completion_rate = round(done_cnt / approved_cnt * 100) if approved_cnt > 0 else 0

    st.progress(
        completion_rate / 100,
        text=f"處置完成率 {completion_rate}%　（已核准 {approved_cnt} 項中，{done_cnt} 已完成、{in_progress_cnt} 進行中、{blocked_cnt} 阻塞、{not_started_cnt} 未開始）",
    )


def _build_action_trend_rows() -> list[dict]:
    """從 analysis_results_cache.json 讀取全部週次的 Action 狀態，回傳已排序的 list[dict]。"""
    cache = _load_analysis_results_cache()
    by_week = cache.get("by_week", {})
    rows = []
    for week_label, record in by_week.items():
        if not isinstance(record, dict):
            continue
        decisions = record.get("exec_decisions", {})
        actions = record.get("exec_actions", {})
        if not isinstance(decisions, dict):
            decisions = {}
        if not isinstance(actions, dict):
            actions = {}

        approved_keys = [k for k, v in decisions.items() if v == "approved"]
        on_hold_cnt = sum(1 for v in decisions.values() if v == "on_hold")
        done = in_progress = blocked = not_started = 0
        for dkey in approved_keys:
            action = actions.get(dkey, {})
            if not isinstance(action, dict):
                action = {}
            s = str(action.get("action_status") or "未開始")
            if s == "已完成":
                done += 1
            elif s == "進行中":
                in_progress += 1
            elif s == "阻塞":
                blocked += 1
            else:
                not_started += 1

        approved_cnt = len(approved_keys)
        completion_rate = round(done / approved_cnt * 100) if approved_cnt > 0 else 0
        rows.append({
            "week_label": week_label,
            "done": done,
            "in_progress": in_progress,
            "blocked": blocked,
            "not_started": not_started,
            "on_hold": on_hold_cnt,
            "completion_rate": completion_rate,
        })

    rows.sort(key=lambda r: _parse_week_num(r["week_label"]) or 0)
    return rows


def _render_action_trend_chart(trend_rows: list[dict]) -> None:
    """渲染每週 Action 狀態堆疊柱狀圖 + 處置完成率趨勢線。"""
    if not trend_rows:
        st.caption("尚無跨週 Action 趨勢資料。")
        return

    x = [r["week_label"] for r in trend_rows]
    done_vals = [r["done"] for r in trend_rows]
    in_progress_vals = [r["in_progress"] for r in trend_rows]
    blocked_vals = [r["blocked"] for r in trend_rows]
    not_started_vals = [r["not_started"] for r in trend_rows]
    on_hold_vals = [r["on_hold"] for r in trend_rows]
    rate_vals = [r["completion_rate"] for r in trend_rows]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(
        go.Bar(x=x, y=done_vals, name="已完成", marker_color="#4CAF50", opacity=0.9),
        secondary_y=False,
    )
    fig.add_trace(
        go.Bar(x=x, y=in_progress_vals, name="進行中", marker_color="#FF9800", opacity=0.9),
        secondary_y=False,
    )
    fig.add_trace(
        go.Bar(x=x, y=blocked_vals, name="阻塞", marker_color="#F44336", opacity=0.9),
        secondary_y=False,
    )
    fig.add_trace(
        go.Bar(x=x, y=not_started_vals, name="未開始", marker_color="#9E9E9E", opacity=0.75),
        secondary_y=False,
    )
    fig.add_trace(
        go.Bar(x=x, y=on_hold_vals, name="暫緩", marker_color="#AB47BC", opacity=0.8),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=rate_vals, name="處置完成率",
            mode="lines+markers+text",
            text=[f"{v}%" for v in rate_vals],
            textposition="top center", textfont=dict(color="cyan"),
            line=dict(color="cyan", width=2), marker=dict(color="cyan", size=8),
        ),
        secondary_y=True,
    )
    fig.add_hline(
        y=80, line_dash="dash", line_color="red",
        annotation_text="Goal 80%", annotation_position="top left",
        secondary_y=True,
    )

    max_bar = max(
        (r["done"] + r["in_progress"] + r["blocked"] + r["not_started"] + r["on_hold"])
        for r in trend_rows
    ) if trend_rows else 5
    fig.update_yaxes(title_text="項數", range=[0, max(max_bar * 1.5, 5)], secondary_y=False)
    fig.update_yaxes(title_text="完成率 (%)", range=[0, 115], secondary_y=True)
    fig.update_xaxes(title_text="Week")
    fig.update_layout(
        title="Weekly Dir Agent Action 處置完成率趨勢",
        barmode="stack",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=500,
    )
    st.plotly_chart(fig, use_container_width=True)


def render_card(title, value, bg_color):
    st.markdown(
        f"""
        <div style="
            background-color:{bg_color};
            color:white;
            padding:16px;
            border-radius:10px;
            text-align:center;
            min-height:90px;
            display:flex;
            flex-direction:column;
            justify-content:center;
        ">
            <div style="font-size:14px;opacity:0.9;">{title}</div>
            <div style="font-size:28px;font-weight:700;line-height:1.2;">{value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ── 讀取 AMD 資料 ────────────────────────────────────────────────────
@st.cache_data
def load_amd_data(week_key: str | None = None):
    week_key = week_key or _current_iso_week_key()

    def _build() -> pd.DataFrame:
        _yield_dir = r"\\khaiotapvp02\AMD\Yield"
        _xlsx_files = glob.glob(os.path.join(_yield_dir, "*.xlsx"))
        if not _xlsx_files:
            raise FileNotFoundError(f"找不到任何 Excel 檔案於：{_yield_dir}")
        file_path = max(_xlsx_files, key=os.path.getmtime)
        df = pd.read_excel(file_path, sheet_name="lot Single yield analysis")

        df["Week"] = df["Week"].bfill()
        # 避免空字串/字串型 null 造成後續週次排序與圖軸異常
        df["Week"] = df["Week"].astype(str).str.strip()
        df["Week"] = df["Week"].replace({"": pd.NA, "nan": pd.NA, "NaN": pd.NA, "None": pd.NA})

        mask = df["Week"].isna()
        for idx in df[mask].index:
            date_val = pd.to_datetime(df.loc[idx, "Date"], errors="coerce")
            if pd.notna(date_val):
                wk = date_val.isocalendar()[1]
                df.loc[idx, "Week"] = f"W{wk:02d}"

        df["Week"] = df["Week"].apply(_normalize_week_label)
        df = df.dropna(subset=["Week"])

        if df["yield"].dtype == object:
            df["yield_pct"] = pd.to_numeric(
                df["yield"].astype(str).str.rstrip("%"), errors="coerce"
            )
        else:
            df["yield_pct"] = df["yield"].apply(
                lambda v: v * 100 if pd.notna(v) and v <= 1 else v
            )

        if df["Particle lost"].dtype == object:
            df["particle_lost_pct"] = pd.to_numeric(
                df["Particle lost"].astype(str).str.rstrip("%"), errors="coerce"
            )
        else:
            df["particle_lost_pct"] = df["Particle lost"].apply(
                lambda v: v * 100 if pd.notna(v) and v <= 1 else v
            )

        df["particle_lost_pct"] = df["particle_lost_pct"].fillna(0)
        df["yield_pct"] = df["yield_pct"].fillna(0)

        df["Date_parsed"] = pd.to_datetime(df["Date"], errors="coerce")
        week_order = df.groupby("Week")["Date_parsed"].min().sort_values().index.tolist()

        def agg_week(g):
            total_die = g["Die"].sum()
            if total_die == 0:
                return pd.Series({"Die Qty": 0, "1P1M Yield": 0, "Particle Yield": 100})
            weighted_yield = (g["yield_pct"] * g["Die"]).sum() / total_die
            weighted_particle_lost = (g["particle_lost_pct"] * g["Die"]).sum() / total_die
            return pd.Series(
                {
                    "Die Qty": int(total_die),
                    "1P1M Yield": round(weighted_yield, 2),
                    "Particle Yield": round(100 - weighted_particle_lost, 2),
                }
            )

        weekly = df.groupby("Week").apply(agg_week).reset_index()
        weekly["Week"] = pd.Categorical(weekly["Week"], categories=week_order, ordered=True)
        weekly = weekly.sort_values("Week").reset_index(drop=True)
        weekly = weekly.tail(17).reset_index(drop=True) #
        return weekly

    # return _get_weekly_snapshot("amd_weekly", week_key, _build)
    return _get_weekly_snapshot("amd_weekly_17w", week_key, _build)


# ── 讀取 KPI 資料 (API) ──────────────────────────────────────────────
@st.cache_data
def load_kpi_data(week_key: str | None = None):
    week_key = week_key or _current_iso_week_key()

    def _build() -> pd.DataFrame:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(weeks=15)).strftime("%Y%m%d")
        params = {
            "start_date": start_date,
            "end_date": end_date,
            "building": "K18-6F",
        }
        try:
            resp = session.get(f"{KPI_API_BASE_URL}/api/weekly-kpi", params=params, timeout=120)

            resp.raise_for_status()
            data = resp.json()
            weeks_df = pd.DataFrame(data["weeks"])
            if weeks_df.empty:
                return weeks_df

            if "week" in weeks_df.columns:
                weeks_df["Week"] = weeks_df["week"].apply(_normalize_week_label)
            elif "week_label" in weeks_df.columns:
                weeks_df["Week"] = weeks_df["week_label"].apply(_normalize_week_label)
            else:
                weeks_df["Week"] = None

            weeks_df = weeks_df.dropna(subset=["Week"])

            weeks_df["oos_event_count"] = pd.to_numeric(
                weeks_df.get("oos_event_count", 0), errors="coerce"
            )
            weeks_df["compliance_rate"] = pd.to_numeric(
                weeks_df.get("compliance_rate", 0), errors="coerce"
            )
            return weeks_df
        except Exception:
            return pd.DataFrame()

    return _get_weekly_snapshot("kpi_weekly", week_key, _build)


@st.cache_data(ttl=600)
def load_ee_latest_week_payload(week_label: str = "", top_k: int = 15) -> dict:
    week_range = _to_year_week(week_label)

    try:
        resp = session.post(
            f"{EE_AGENT_API_BASE_URL}/latest-week",
            json={
                "week_range": week_range,
                "top_k": top_k,
                "use_langfuse": False,
                "include_trace": True,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        output = (data.get("output") or "").strip() or "EE Agent 無回傳內容"
        return {
            "output": output,
            "trace": data.get("trace") if isinstance(data.get("trace"), dict) else {},
        }
    except Exception as e:
        return {
            "output": f"EE Agent API 呼叫失敗：{str(e)}",
            "trace": {
                "error": str(e),
            },
        }


def load_ee_latest_week_report(week_label: str = "", top_k: int = 15) -> str:
    payload = load_ee_latest_week_payload(week_label=week_label, top_k=top_k)
    return str(payload.get("output") or "EE Agent 無回傳內容")


def load_ee_latest_week_trace(week_label: str = "", top_k: int = 15) -> dict:
    payload = load_ee_latest_week_payload(week_label=week_label, top_k=top_k)
    trace = payload.get("trace")
    return trace if isinstance(trace, dict) else {}


def _shorten_text(text: str, limit: int = 500) -> str:
    if text is None:
        return ""
    value = str(text)
    return value if len(value) <= limit else (value[:limit] + " ...")


def _safe_parse_obj(raw_value):
    if isinstance(raw_value, (dict, list)):
        return raw_value
    if raw_value is None:
        return {}

    raw_text = str(raw_value).strip()
    if not raw_text:
        return {}

    try:
        return json.loads(raw_text)
    except Exception:
        pass

    try:
        return ast.literal_eval(raw_text)
    except Exception:
        return {}


def _extract_new_items_from_trace(trace_data: dict) -> list:
    events = trace_data.get("events", []) if isinstance(trace_data, dict) else []
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        if event.get("event") != "run_result_snapshot":
            continue
        snapshot = (event.get("data") or {}).get("snapshot")
        if isinstance(snapshot, dict):
            new_items = snapshot.get("new_items", [])
            if isinstance(new_items, list):
                return _attach_step_timestamps_from_events(trace_data, new_items)
    # 快速路徑（/api/weekly-decision-fast 直接 LLM 合成）不會產生 run_result_snapshot，
    # 改由 Orchestrator 事件序列合成可顯示步驟。
    return _synthesize_items_from_orchestrator_events(trace_data)


def _synthesize_items_from_orchestrator_events(trace_data: dict) -> list:
    """Orchestrator 快速決策路徑沒有 run_result_snapshot，改用 orchestrator
    事件序列合成 Step-by-Step 可顯示項目。"""
    if not isinstance(trace_data, dict):
        return []
    events = trace_data.get("events", [])
    if not isinstance(events, list) or not events:
        return []

    def _reasoning(text: str, ts: str) -> dict:
        return {
            "type": "reasoning_item",
            "raw_item": {"content": [{"type": "text", "text": text}]},
            "ts": ts,
        }

    items: list[dict] = []
    has_orchestrator_signal = False

    for ev in events:
        if not isinstance(ev, dict):
            continue
        name = str(ev.get("event") or "")
        ts = str(ev.get("ts") or "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

        if name == "action_items_fetched":
            has_orchestrator_signal = True
            items.append({
                "type": "tool_call_item",
                "name": "fetch_weekly_meeting_records",
                "arguments": {"week_tag": data.get("week_tag", "")},
                "ts": ts,
            })
            items.append({
                "type": "tool_call_output_item",
                "output": {
                    "week_tag": data.get("week_tag", ""),
                    "record_count": data.get("record_count", 0),
                },
                "ts": ts,
            })
        elif name == "orchestrator_conflict_detection":
            has_orchestrator_signal = True
            conflict_machines = data.get("conflict_machines") or []
            lines = [
                "跨部門衝突偵測（規則：PE 懷疑且 EE 未示警）",
                f"- 是否偵測到衝突：{'是' if data.get('conflict_detected') else '否'}",
                f"- 衝突機台數：{data.get('conflict_count', 0)}",
                f"- 衝突機台：{', '.join(conflict_machines) or '無'}",
                f"- EE/PE 共識高風險機台：{', '.join(data.get('consensus_machines') or []) or '無'}",
                f"- EE 風險機台：{', '.join(data.get('ee_risk_machines') or []) or '無'}",
                f"- PE 懷疑機台：{', '.join(data.get('pe_suspect_machines') or []) or '無'}",
            ]
            items.append(_reasoning("\n".join(lines), ts))
        elif name == "qa_deep_dive_triggered":
            has_orchestrator_signal = True
            items.append({
                "type": "tool_call_item",
                "name": "search_contamination_analysis",
                "arguments": {"conflict_machines": data.get("conflict_machines") or []},
                "ts": ts,
            })
            items.append({
                "type": "tool_call_output_item",
                "output": str(data.get("deep_dive_preview") or ""),
                "ts": ts,
            })
        elif name == "qa_deep_dive_skipped":
            has_orchestrator_signal = True
            items.append(_reasoning(
                f"QA 深度追查略過（原因：{data.get('reason', '無衝突')}）。", ts))
        elif name == "qa_deep_dive_no_result":
            has_orchestrator_signal = True
            items.append(_reasoning("QA 深度追查已觸發但未取得有效結果。", ts))
        elif name == "orchestrator_trace":
            has_orchestrator_signal = True
            stages = data.get("stages") or []
            stage_lines = ["Orchestrator 管線階段："]
            for s in stages:
                if isinstance(s, dict):
                    stage_lines.append(f"- [{s.get('ts', '')}] {s.get('stage', '')}")
            total_ms = data.get("total_elapsed_ms")
            if total_ms is not None:
                stage_lines.append(f"\n總耗時：{total_ms} ms")
            items.append(_reasoning("\n".join(stage_lines), ts))
        elif name == "direct_llm_synthesis_used":
            has_orchestrator_signal = True
            length = data.get("report_length")
            elapsed = data.get("elapsed_ms")
            note = "整合決策報告已由 Orchestrator 直接 LLM 合成完成。"
            if length is not None:
                note += f"\n報告長度：約 {length} 字。"
            if elapsed is not None:
                note += f"\n總耗時：{elapsed} ms。"
            note += "\n完整報告內容請見上方「整合決策報告」分頁。"
            items.append({
                "type": "message_output_item",
                "raw_item": {"content": [{"type": "output_text", "text": note}]},
                "ts": ts,
            })
        elif name == "runner_error":
            has_orchestrator_signal = True
            items.append(_reasoning(f"執行發生錯誤：{data.get('error', '')}", ts))

    return items if has_orchestrator_signal else []


def _attach_step_timestamps_from_events(trace_data: dict, items: list) -> list:
    """當 new_items 缺少 ts 時，改用 events 依序回填 Step 時間。"""
    if not isinstance(trace_data, dict) or not isinstance(items, list):
        return items if isinstance(items, list) else []

    events = trace_data.get("events", [])
    if not isinstance(events, list):
        return items

    tool_start_ts = []
    tool_done_ts = []
    runner_done_ts = ""
    snapshot_ts = ""

    for ev in events:
        if not isinstance(ev, dict):
            continue
        event_name = str(ev.get("event") or "")
        ts = str(ev.get("ts") or "").strip()
        if not ts:
            continue

        if event_name == "tool_start":
            tool_start_ts.append(ts)
        elif event_name == "tool_done":
            tool_done_ts.append(ts)
        elif event_name == "runner_done":
            runner_done_ts = ts
        elif event_name == "run_result_snapshot":
            snapshot_ts = ts

    start_idx = 0
    done_idx = 0
    enriched_items = []

    for item in items:
        if not isinstance(item, dict):
            enriched_items.append(item)
            continue

        if str(item.get("ts") or "").strip():
            enriched_items.append(item)
            continue

        item_type = str(item.get("type") or "")
        enriched = dict(item)

        if item_type == "tool_call_item" and start_idx < len(tool_start_ts):
            enriched["ts"] = tool_start_ts[start_idx]
            start_idx += 1
        elif item_type == "tool_call_output_item" and done_idx < len(tool_done_ts):
            enriched["ts"] = tool_done_ts[done_idx]
            done_idx += 1
        elif item_type == "message_output_item":
            enriched["ts"] = runner_done_ts or snapshot_ts
        elif item_type == "reasoning_item":
            if start_idx < len(tool_start_ts):
                enriched["ts"] = tool_start_ts[start_idx]
            elif done_idx > 0 and (done_idx - 1) < len(tool_done_ts):
                enriched["ts"] = tool_done_ts[done_idx - 1]
            elif runner_done_ts:
                enriched["ts"] = runner_done_ts

        enriched_items.append(enriched)

    return enriched_items


def _normalize_tool_args(args_value) -> str:
    if isinstance(args_value, (dict, list)):
        return json.dumps(args_value, ensure_ascii=False, indent=2)
    parsed = _safe_parse_obj(args_value)
    if isinstance(parsed, (dict, list)) and parsed:
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    return str(args_value or "")


def _extract_reasoning_text(item: dict) -> str:
    raw_obj = _safe_parse_obj(item.get("raw_item", item))
    if isinstance(raw_obj, dict):
        texts = []
        content = raw_obj.get("content", []) or []
        content = _safe_parse_obj(content)
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            content = []

        for c in content:
            c = _safe_parse_obj(c)
            if isinstance(c, dict) and c.get("type") in ("reasoning_text", "text"):
                t = c.get("text")
                if t:
                    texts.append(str(t))

        summary = _safe_parse_obj(raw_obj.get("summary", ""))
        if isinstance(summary, list):
            for s in summary:
                s = _safe_parse_obj(s)
                if isinstance(s, dict):
                    t = s.get("text")
                    if t:
                        texts.append(str(t))

        if texts:
            return "\n\n".join(texts)
    return str(item.get("raw_item") or item or "")


def _extract_tool_call(item: dict) -> tuple[str, str]:
    if isinstance(item, dict):
        inline_tool_name = item.get("name") or item.get("tool_name")
        if inline_tool_name:
            return str(inline_tool_name), _normalize_tool_args(item.get("arguments", ""))

    raw_obj = _safe_parse_obj(item.get("raw_item", item))
    if isinstance(raw_obj, dict):
        tool_name = raw_obj.get("name") or raw_obj.get("tool_name") or "(unknown_tool)"
        args = raw_obj.get("arguments", "")
        return str(tool_name), _normalize_tool_args(args)
    return "(unknown_tool)", str(item.get("raw_item") or "")


def _canonical_trace_item_type(item_type: str) -> str:
    t = str(item_type or "").strip().lower()
    mapping = {
        "reasoning": "reasoning_item",
        "reasoning_item": "reasoning_item",
        "function_call": "tool_call_item",
        "tool_call": "tool_call_item",
        "tool_call_item": "tool_call_item",
        "function_call_output": "tool_call_output_item",
        "tool_call_output": "tool_call_output_item",
        "tool_call_output_item": "tool_call_output_item",
        "message": "message_output_item",
        "message_output_item": "message_output_item",
    }
    return mapping.get(t, str(item_type or "unknown"))


def _get_tool_description(tool_name: str) -> str:
    """回傳工具用途描述（精簡版），供 trace 顯示。"""
    tool_desc_map = {
        # EE Agent tools
        "get_latest_weekly_overall_report": "取得每週 Top5 機台集中性分析的熱點機台與風險指標，  \nData source：[APX System] Top5 機台集中性分析",

        "get_rework_weekly_data": "取得每周的 Rework 趨勢資料，包含各 Layer 趨勢與過去的週次摘要，  \nData source：[PE] Weisshorn Rework Data",

        "detect_rework_bypass_risk": "偵測 OP 漏檢(BY PASS)風險，回傳風險警示與分析摘要。  \nData source：[PE] Weisshorn Rework Data",

        # PE Agent tools
        "search_lots_by_yield_loss": "取得高 Particle Yield loss Lot 的批次資訊與追蹤這些 Lot 製程經過哪些機台，  \nData source：[MES] TRACK 批號機台生產紀錄、[EdgeAI] PSN OOB 異常紀錄、[MES] Routing 產品製程路徑",

        "get_amd_yield_summary_by_week": "取得每周的良率報告（總產量、平均良率、Particle Yield 等）。  \nData source：[PE] RDL Yield Weisshorn Data",

        # QA Agent tools
        "search_weekly_report": "查詢指定週次 OOS PM 保養週報（每部機台的保養情況/成功率/失敗率）。  \nData source：[APX System] OOS 保養紀錄管理整合分析系統",

        "list_weekly_reports": "列出每週 OOS PM 成效 (保養次數/有效率)，供跨週保養趨勢比較。  \nData source：[APX System] OOS 保養紀錄管理整合分析系統",

        "search_tool_trajectory": "追蹤機台的狀態是趨勢改善中/惡化中/長期無效/邊際遞減(越來越差)。  \nData source：[APX System] OOS 保養紀錄管理整合分析系統",

        "search_qa_semantic": "以自然語言方式搜尋 OOS PM 機台保養報告。  \nData source：[APX System] OOS 保養紀錄管理整合分析系統",

        "search_contamination_analysis": "查詢炭膠帶污染源分析結果（機台汙染源、可疑物、信心度）。  \nData source：[QA] 炭膠帶 / EDX 分析結果",

        "cross_reference_qa_contamination": "交叉比對機台保養成效與炭膠帶污染源分析結果，分析機台保養無效原因。  \nData source：[QA] 炭膠帶 / EDX 分析結果、[APX System] OOS 保養紀錄管理整合分析系統",

        # Executive Agent tools
        "get_ee_weekly_report": "讀取 EE 設備工程本週週報（由 UI 預先載入）。  \nData source：[EE Agent] 每週 EE Agent 分析報告",

        "get_qa_weekly_report": "讀取 QA 品質保證部本週週報（由 UI 預先載入）。  \nData source：[QA Agent] 每週 QA Agent 分析報告",

        "get_pe_weekly_report": "讀取 PE 製程工程本週週報（由 UI 預先載入）。  \nData source：[PE Agent] 每週 PE Agent 分析報告",

        "fetch_weekly_meeting_records": "查詢指定週次會議紀錄（Owner、Issue/Due Date、Action）。  \nData source：[EE/PE/QA] 每週 Particle Control 會議紀錄",

        "compare_product_equipment": "比較產品機台差異（如 Cayman vs AMD）並提供限機策略建議。",

        "query_weekly_report_context": "語意搜尋歷史週報向量庫，取得指定週次 EE/QA/PE/Dir 週報相關片段。  \nData source：[Qdrant] BP_APX_WEEKLY_REPORTS 向量庫"}
    return tool_desc_map.get(tool_name, "此工具描述尚未定義。")


def _extract_tool_output_preview(item: dict, tool_name: str = "") -> str:
    output = item.get("output", "")
    parsed = _safe_parse_obj(output)
    if isinstance(parsed, dict) and parsed:
        # 依工具名稱顯示對應摘要，避免不同工具共用欄位造成誤解。
        if tool_name == "get_rework_weekly_data":
            preview = {
                "week_range": parsed.get("week_range"),
                "weeks_analyzed": parsed.get("weeks_analyzed") or [],
                "detail": parsed.get("detail"),
                "layer_trends": (parsed.get("layer_trends") or [])[:3],
            }
            return json.dumps(preview, ensure_ascii=False, indent=2)

        if tool_name == "detect_rework_bypass_risk":
            preview = {
                "week_range": parsed.get("week_range"),
                "has_risk": parsed.get("has_risk"),
                "alerts": (parsed.get("alerts") or [])[:5],
                "summary": parsed.get("summary"),
            }
            return json.dumps(preview, ensure_ascii=False, indent=2)

        if tool_name == "get_latest_weekly_overall_report":
            preview = {
                "week_range": parsed.get("week_range"),
                "summary": parsed.get("summary"),
                "top_hotspot": (parsed.get("ranked_hotspots") or [])[:3],
            }
            return json.dumps(preview, ensure_ascii=False, indent=2)

        # 其他工具採通用安全預覽。
        preview = {
            "week_range": parsed.get("week_range"),
            "summary": parsed.get("summary") if "summary" in parsed else None,
            "keys": list(parsed.keys())[:10],
        }
        return json.dumps(preview, ensure_ascii=False, indent=2)
    return str(output or "")


def _extract_final_output(item: dict) -> str:
    raw_obj = _safe_parse_obj(item.get("raw_item", item))
    if isinstance(raw_obj, dict):
        contents = _safe_parse_obj(raw_obj.get("content", []) or [])
        if isinstance(contents, dict):
            contents = [contents]
        if not isinstance(contents, list):
            contents = []

        texts = []
        for c in contents:
            c = _safe_parse_obj(c)
            if not isinstance(c, dict):
                continue
            txt = c.get("text")
            if txt:
                texts.append(str(txt))
            elif c.get("type") == "output_text" and c.get("text"):
                texts.append(str(c.get("text")))

        # 某些格式會直接把最終文字放在 message.text
        direct_text = raw_obj.get("text")
        if direct_text:
            texts.append(str(direct_text))

        if texts:
            return "\n\n".join(texts)
    return str(item.get("raw_item") or item or "")


def _extract_item_ts(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    ts = item.get("ts")
    return str(ts).strip() if ts is not None else ""


def _build_trace_timeline(trace_data: dict) -> list[dict]:
    """將 trace events 轉為時序圖區段（Session Replay）。

    回傳每段 {category, label, start, end, detail}，
    時間以秒為單位，從第一個事件起算。
    """
    if not isinstance(trace_data, dict):
        return []
    events = trace_data.get("events", [])
    if not isinstance(events, list) or not events:
        return []

    def _parse_ts(value):
        try:
            return datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None

    parsed = [
        (_parse_ts(ev.get("ts")), ev)
        for ev in events
        if isinstance(ev, dict)
    ]
    parsed = [(t, ev) for t, ev in parsed if t is not None]
    if not parsed:
        return []

    parsed.sort(key=lambda pair: pair[0])
    t0 = parsed[0][0]

    def _offset(t) -> float:
        return (t - t0).total_seconds()

    segments: list[dict] = []
    pending_tools: list[tuple[str, datetime]] = []
    last_activity_end = None

    for t, ev in parsed:
        name = str(ev.get("event") or "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

        if name == "runner_start":
            last_activity_end = t
        elif name == "tool_start":
            if last_activity_end is not None and (t - last_activity_end).total_seconds() > 0.01:
                segments.append({
                    "category": "LLM",
                    "label": "LLM 推理",
                    "start": _offset(last_activity_end),
                    "end": _offset(t),
                    "detail": "模型思考 / 規劃下一步工具呼叫",
                })
            pending_tools.append((str(data.get("tool") or "tool"), t))
            last_activity_end = t
        elif name in ("tool_done", "tool_error"):
            tool_name = str(data.get("tool") or "tool")
            start_t = None
            for i in range(len(pending_tools) - 1, -1, -1):
                if pending_tools[i][0] == tool_name:
                    start_t = pending_tools.pop(i)[1]
                    break
            if start_t is None and pending_tools:
                start_t = pending_tools.pop()[1]

            elapsed_ms = data.get("elapsed_ms")
            if start_t is not None:
                start_off = _offset(start_t)
            elif elapsed_ms is not None:
                try:
                    start_off = _offset(t) - float(elapsed_ms) / 1000.0
                except (ValueError, TypeError):
                    start_off = _offset(t)
            else:
                start_off = _offset(t)

            is_error = name == "tool_error"
            detail = data.get("error") if is_error else data.get("output_preview")
            segments.append({
                "category": "Error" if is_error else "Tool",
                "label": tool_name,
                "start": max(start_off, 0.0),
                "end": _offset(t),
                "detail": str(detail or "")[:300],
            })
            last_activity_end = t
        elif name == "runner_done":
            if last_activity_end is not None and (t - last_activity_end).total_seconds() > 0.01:
                segments.append({
                    "category": "LLM",
                    "label": "LLM 生成回覆",
                    "start": _offset(last_activity_end),
                    "end": _offset(t),
                    "detail": "整合工具結果並生成最終報告",
                })
            last_activity_end = t

    return sorted(segments, key=lambda s: (s["start"], s["end"]))


def _build_timeline_from_orchestrator_stages(trace_data: dict) -> list[dict]:
    """快速路徑無 tool_start/tool_done 事件，改用 orchestrator_trace 的 stages
    時間戳建立 Session Replay 時序圖。"""
    events = trace_data.get("events", []) if isinstance(trace_data, dict) else []
    stages: list = []
    for ev in events:
        if isinstance(ev, dict) and ev.get("event") == "orchestrator_trace":
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            raw_stages = data.get("stages")
            if isinstance(raw_stages, list):
                stages = raw_stages
            break
    if not stages:
        return []

    def _parse_ts(value):
        try:
            return datetime.fromisoformat(str(value))
        except (ValueError, TypeError):
            return None

    ts_by_stage: dict[str, datetime] = {}
    times: list[datetime] = []
    for s in stages:
        if not isinstance(s, dict):
            continue
        t = _parse_ts(s.get("ts"))
        if t is None:
            continue
        ts_by_stage[str(s.get("stage") or "")] = t
        times.append(t)
    if not times:
        return []

    t0 = min(times)

    def _off(t) -> float:
        return (t - t0).total_seconds()

    pairs = [
        ("Tool", "衝突偵測", "pipeline_from_reports_start", "conflict_detection_done", "EE/PE 跨部門衝突偵測"),
        ("Tool", "QA 深度追查", "qa_deep_dive_start", "qa_deep_dive_done", "炭膠帶污染源微觀追查"),
        ("LLM", "Executive LLM 合成", "executive_llm_start", "executive_llm_done", "整合三部門報告，產出決策"),
    ]
    segments: list[dict] = []
    for cat, label, start_key, end_key, detail in pairs:
        st_t = ts_by_stage.get(start_key)
        en_t = ts_by_stage.get(end_key)
        if st_t is None or en_t is None:
            continue
        segments.append({
            "category": cat,
            "label": label,
            "start": max(_off(st_t), 0.0),
            "end": _off(en_t),
            "detail": detail,
        })
    return sorted(segments, key=lambda s: (s["start"], s["end"]))


def _render_trace_timeline(agent_name: str, trace_data: dict) -> None:
    """以時序甘特圖（Session Replay）呈現 Agent 執行過程。"""
    segments = _build_trace_timeline(trace_data)
    if not segments:
        segments = _build_timeline_from_orchestrator_stages(trace_data)
    if not segments:
        return

    color_map = {"Tool": "#FFB300", "Error": "#F44336", "LLM": "#26C6DA"}
    category_label = {"Tool": "Tool", "Error": "Error", "LLM": "LLM"}
    total = len(segments)
    total_span = max((s["end"] for s in segments), default=0.0)

    fig = go.Figure()
    for cat in ("Tool", "Error", "LLM"):
        xs, bases, ys, texts, hovers = [], [], [], [], []
        for idx, seg in enumerate(segments):
            if seg["category"] != cat:
                continue
            duration = max(seg["end"] - seg["start"], 0.0)
            xs.append(max(duration, total_span * 0.004 if total_span else 0.05))
            bases.append(seg["start"])
            ys.append(total - idx)  # 由上而下依時間排序
            texts.append(seg["label"])
            detail = f"<br>{seg['detail']}" if seg["detail"] else ""
            hovers.append(
                f"<b>{seg['label']}</b><br>類型: {category_label[cat]}"
                f"<br>起迄: {seg['start']:.2f}s → {seg['end']:.2f}s"
                f"<br>耗時: {duration:.2f}s{detail}"
            )
        if not xs:
            continue
        fig.add_trace(go.Bar(
            x=xs,
            base=bases,
            y=ys,
            orientation="h",
            name=category_label[cat],
            marker_color=color_map[cat],
            text=texts,
            textposition="inside",
            insidetextanchor="start",
            textangle=0,
            textfont=dict(color="white", size=20),
            constraintext="none",
            cliponaxis=False,
            hovertext=hovers,
            hoverinfo="text",
            width=0.62,
        ))

    fig.update_layout(
        title=f"{agent_name} Session Replay（執行時序圖）",
        barmode="overlay",
        bargap=0.35,
        height=max(260, 42 * total + 130),
        xaxis=dict(title="時間 (秒，從第一個事件起算)", showgrid=True, zeroline=True),
        yaxis=dict(showticklabels=False, showgrid=False),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=20, r=20, t=60, b=40),
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_trace_panel(agent_name: str, trace_data: dict) -> None:
    st.markdown(f"#### {agent_name} Execution Trace")
    with st.container(border=True):
        with st.expander("完整 Trace JSON", expanded=False):
            if trace_data:
                st.json(trace_data, expanded=False)
            else:
                st.caption("目前無 trace 原始資料")

        _render_trace_timeline(agent_name, trace_data)

        items = _extract_new_items_from_trace(trace_data)
        if not items:
            st.caption("目前無可顯示的 trace 資料")
            return

        # ── 顯示 User Prompt ────────────────────────────────────────
        user_prompt = ""
        if isinstance(trace_data, dict):
            for ev in trace_data.get("events", []):
                if isinstance(ev, dict) and ev.get("event") == "runner_start":
                    user_prompt = str((ev.get("data") or {}).get("prompt") or "")
                    break
            if not user_prompt:
                req = trace_data.get("request", {})
                user_prompt = str(req.get("query") or req.get("week_range") or "")

        if user_prompt:
            with st.chat_message("user"):
                st.markdown("**User Prompt**")
                st.code(user_prompt, language="text", wrap_lines=True)

        st.markdown("#### Agent Step-by-Step")

        # 先把最終輸出抓出來，流程區只呈現 reasoning / tool 相關步驟
        final_outputs = []
        for item in items:
            if _canonical_trace_item_type(str(item.get("type") or "")) == "message_output_item":
                final_outputs.append(_extract_final_output(item))

        flow_items = [
            item for item in items
            if _canonical_trace_item_type(str(item.get("type") or "")) != "message_output_item"
        ]

        current_tool_name = ""
        for idx, item in enumerate(flow_items, start=1):
            raw_item_type = str(item.get("type") or "unknown")
            item_type = _canonical_trace_item_type(raw_item_type)
            item_ts = _extract_item_ts(item)

            if item_type == "reasoning_item":
                with st.chat_message("assistant"):
                    st.markdown(f"**Step {idx} | Reasoning**")
                    if item_ts:
                        st.caption(f"ts: {item_ts}")
                    st.code(
                        _shorten_text(_extract_reasoning_text(item), 1200),
                        language="text",
                        wrap_lines=True,
                    )

            elif item_type == "tool_call_item":
                tool_name, tool_args = _extract_tool_call(item)
                current_tool_name = str(tool_name or "")
                with st.chat_message("assistant"):
                    st.markdown(f"**Step {idx} | Tool Call**")
                    if item_ts:
                        st.caption(f"ts: {item_ts}")
                    st.markdown(f"Tool: {tool_name}")
                    st.caption(f"Description: {_shorten_text(_get_tool_description(tool_name), 300)}")
                    st.code(
                        _shorten_text(tool_args, 1500),
                        language="json",
                        wrap_lines=True,
                    )

            elif item_type == "tool_call_output_item":
                with st.chat_message("assistant"):
                    st.markdown(f"**Step {idx} | Tool Output**")
                    if item_ts:
                        st.caption(f"ts: {item_ts}")
                    st.code(
                        _shorten_text(_extract_tool_output_preview(item, current_tool_name), 1800),
                        language="json",
                        wrap_lines=True,
                    )

            else:
                with st.chat_message("assistant"):
                    st.markdown(f"**Step {idx} | {raw_item_type}**")
                    if item_ts:
                        st.caption(f"ts: {item_ts}")
                    st.code(
                        _shorten_text(str(item), 1200),
                        language="text",
                        wrap_lines=True,
                    )

        st.markdown("#### Final Result")
        final_text = "\n\n".join([t for t in final_outputs if t]).strip()
        with st.chat_message("assistant"):
            final_ts = ""
            for item in reversed(items):
                if _canonical_trace_item_type(str(item.get("type") or "")) == "message_output_item":
                    final_ts = _extract_item_ts(item)
                    if final_ts:
                        break
            if final_ts:
                st.caption(f"ts: {final_ts}")
            if final_text:
                st.markdown(_shorten_text(final_text, 6000))
            else:
                st.caption("目前無最終輸出可顯示")


def _get_iso_week_date_range(week_label: str) -> tuple[str | None, str | None]:
    if not isinstance(week_label, str) or not week_label.startswith("W"):
        return None, None
    try:
        week_num = int(week_label[1:])
        year = datetime.now().isocalendar().year
        start = datetime.fromisocalendar(year, week_num, 1).date()
        end = datetime.fromisocalendar(year, week_num, 7).date()
        return start.isoformat(), end.isoformat()
    except Exception:
        return None, None


@st.cache_data(ttl=600)
def load_pe_latest_week_payload(week_label: str = "", yield_data: dict | None = None) -> dict:
    start_date, end_date = _get_iso_week_date_range(week_label)
    payload = {
        "min_yield_loss": 0.001,
        "limit": 30,
        "pe_note": "AMD Dashboard 自動摘要",
        "use_langfuse": False,
        "include_trace": True,
    }
    if start_date and end_date:
        payload["start_date"] = start_date
        payload["end_date"] = end_date
    if yield_data:
        payload["yield_data"] = yield_data

    try:
        resp = session.post(
            f"{PE_AGENT_API_BASE_URL}/api/weekly-report",
            json=payload,
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        output = (data.get("report") or data.get("answer") or "").strip() or "PE Agent 無回傳內容"
        return {
            "output": output,
            "trace": data.get("trace") if isinstance(data.get("trace"), dict) else {},
        }
    except Exception as e:
        return {
            "output": f"PE Agent API 呼叫失敗：{str(e)}",
            "trace": {
                "error": str(e),
            },
        }


def load_pe_latest_week_report(week_label: str = "", yield_data: dict | None = None) -> str:
    payload = load_pe_latest_week_payload(week_label=week_label, yield_data=yield_data)
    return str(payload.get("output") or "PE Agent 無回傳內容")


def load_pe_latest_week_trace(week_label: str = "", yield_data: dict | None = None) -> dict:
    payload = load_pe_latest_week_payload(week_label=week_label, yield_data=yield_data)
    trace = payload.get("trace")
    return trace if isinstance(trace, dict) else {}


@st.cache_data(ttl=600)
def load_qa_latest_week_payload(week_label: str = "") -> dict:
    """呼叫 QA Agent API 產生指定週次的保養成效週報（含 trace）。"""
    year_week = _to_year_week(week_label)

    if not year_week:
        return {
            "output": "QA Agent 無法產生週報（未提供週次）",
            "trace": {},
        }

    try:
        resp = session.get(
            f"{QA_AGENT_API_BASE_URL}/generate-report/{year_week}",
            params={
                "include_trace": "true",
                "use_langfuse": "false",
            },
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        output = (data.get("answer") or data.get("report") or "").strip() or "QA Agent 無回傳內容"
        return {
            "output": output,
            "trace": data.get("trace") if isinstance(data.get("trace"), dict) else {},
        }
    except Exception as e:
        return {
            "output": f"QA Agent API 呼叫失敗：{str(e)}",
            "trace": {
                "error": str(e),
            },
        }


def load_qa_latest_week_report(week_label: str = "") -> str:
    payload = load_qa_latest_week_payload(week_label=week_label)
    return str(payload.get("output") or "QA Agent 無回傳內容")


def load_qa_latest_week_trace(week_label: str = "") -> dict:
    payload = load_qa_latest_week_payload(week_label=week_label)
    trace = payload.get("trace")
    return trace if isinstance(trace, dict) else {}


def _apply_decision_review(
    report: str,
    iso_week: str,
    exec_trace: dict,
) -> tuple[str, dict]:
    """串接 AI Decision Reviewer（/api/review-decision）：審查 ## 四 並覆蓋回填（走 A）。
    審查失敗時回傳原報告，不阻斷主流程；審查 trace 事件會併入 exec_trace 供 Session Replay 顯示。
    """
    if not report or not report.strip():
        return report, exec_trace
    try:
        resp = session.post(
            f"{EXEC_AGENT_API_BASE_URL}/api/review-decision",
            json={
                "report": report,
                "week_range": iso_week,
                "include_trace": True,
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        reviewed = (data.get("reviewed_report") or "").strip()
        review_trace = data.get("trace") if isinstance(data.get("trace"), dict) else {}

        # 將 Reviewer 的 swarm 事件併入 exec_trace，讓既有 Session Replay 一併呈現審查步驟
        merged_trace = exec_trace if isinstance(exec_trace, dict) else {}
        if review_trace.get("events"):
            merged_events = list(merged_trace.get("events", []))
            merged_events.extend(review_trace["events"])
            merged_trace = {**merged_trace, "events": merged_events}

        return (reviewed or report), merged_trace
    except Exception as e:
        merged_trace = exec_trace if isinstance(exec_trace, dict) else {}
        merged_events = list(merged_trace.get("events", []))
        merged_events.append(
            {"ts": datetime.now().isoformat(), "event": "reviewer_error", "data": {"error": str(e)}}
        )
        return report, {**merged_trace, "events": merged_events}


@st.cache_data(ttl=600, show_spinner=False)
def load_executive_decision_payload(
    week_label: str = "",
    ee_report: str = "",
    qa_report: str = "",
    pe_report: str = "",
) -> dict:
    """呼叫 Executive Agent API 產生跨部門整合決策報告。
    若三份部門報告均已提供，走快速路徑（/api/weekly-decision-fast）跳過重複取報告。
    否則走 /chat 端點，由 Agent 自行以工具（query_weekly_report_context 等）補齊報告。
    產報告後串接 AI Decision Reviewer（/api/review-decision）審查 ## 四 並覆蓋回填。
    """
    # 快速路徑：三份報告都有，直接傳入
    if ee_report.strip() and qa_report.strip() and pe_report.strip():
        iso_week = _to_year_week(week_label)
        try:
            resp = session.post(
                f"{EXEC_AGENT_API_BASE_URL}/api/weekly-decision-fast",
                json={
                    "ee_report": ee_report,
                    "qa_report": qa_report,
                    "pe_report": pe_report,
                    "week_range": iso_week,
                    "include_trace": True,
                },
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
            output = (data.get("report") or "").strip()
            trace = data.get("trace") if isinstance(data.get("trace"), dict) else {}
            output, trace = _apply_decision_review(output, iso_week, trace)
            return {
                "output": output if output else "Executive Agent 無回傳內容",
                "trace": trace,
            }
        except Exception as e:
            return {
                "output": f"Executive Agent API（快速路徑）呼叫失敗：{str(e)}",
                "trace": {"error": str(e)},
            }

    # 原始路徑：三份報告未備齊，改走 /chat 由 Agent 自行以工具補齊三部門報告
    iso_week = _to_year_week(week_label) if isinstance(week_label, str) else ""
    week_hint = iso_week or (week_label or "最新")
    query = (
        f"請產出本週（{week_hint}）的製造高階主管跨部門整合決策報告。\n"
        f"由於三部門本週報告未預先載入，請呼叫 query_weekly_report_context "
        f"以語意搜尋補齊 EE / PE / QA 週報內容（year_week={iso_week or '請自行推算'}），"
        f"並呼叫 fetch_weekly_meeting_records 取得上一週的會議 action items，"
        f"再依系統規範產出完整的五章節整合決策報告。"
    )
    try:
        resp = session.post(
            f"{EXEC_AGENT_API_BASE_URL}/chat",
            json={
                "query": query,
                "include_trace": True,
            },
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        output = (data.get("answer") or "").strip()
        trace = data.get("trace") if isinstance(data.get("trace"), dict) else {}
        output, trace = _apply_decision_review(output, iso_week, trace)
        return {
            "output": output if output else "Executive Agent 無回傳內容",
            "trace": trace,
        }
    except Exception as e:
        return {
            "output": f"Executive Agent API 呼叫失敗：{str(e)}",
            "trace": {"error": str(e)},
        }


def load_executive_decision_report(
    week_label: str = "",
    ee_report: str = "",
    qa_report: str = "",
    pe_report: str = "",
) -> str:
    payload = load_executive_decision_payload(
        week_label=week_label,
        ee_report=ee_report,
        qa_report=qa_report,
        pe_report=pe_report,
    )
    return str(payload.get("output") or "Executive Agent 無回傳內容")


def load_executive_decision_trace(
    week_label: str = "",
    ee_report: str = "",
    qa_report: str = "",
    pe_report: str = "",
) -> dict:
    payload = load_executive_decision_payload(
        week_label=week_label,
        ee_report=ee_report,
        qa_report=qa_report,
        pe_report=pe_report,
    )
    trace = payload.get("trace")
    return trace if isinstance(trace, dict) else {}


def _get_query_param_str(name: str, default: str = "") -> str:
    value = st.query_params.get(name, default)
    if isinstance(value, list):
        return str(value[0]) if value else default
    return str(value)


def _build_ee_report_link(week_label: str) -> str:
    return f"?view=ee-report&week={week_label}"


def _build_ee_trace_link(week_label: str) -> str:
    return f"?view=ee-trace&week={week_label}"


def _build_pe_report_link(week_label: str) -> str:
    return f"?view=pe-report&week={week_label}"


def _build_pe_trace_link(week_label: str) -> str:
    return f"?view=pe-trace&week={week_label}"


def _build_qa_report_link(week_label: str) -> str:
    return f"?view=qa-report&week={week_label}"


def _build_qa_trace_link(week_label: str) -> str:
    return f"?view=qa-trace&week={week_label}"


def _build_exec_report_link(week_label: str) -> str:
    return f"?view=exec-report&week={week_label}"


def _build_exec_trace_link(week_label: str) -> str:
    return f"?view=exec-trace&week={week_label}"


def _build_exec_chat_link(week_label: str) -> str:
    return f"?view=exec-chat&week={week_label}"


def _build_week_options_with_next(weeks: list[str]) -> tuple[list[str], str | None]:
    """建立週次下拉選單（僅顯示實際有資料的週次，反轉為新到舊）。"""
    normalized_weeks = []
    for w in weeks:
        week_label = _normalize_week_label(w)
        if week_label and week_label not in normalized_weeks:
            normalized_weeks.append(week_label)

    if not normalized_weeks:
        return [], None

    option_list = list(reversed(normalized_weeks))
    default_week = option_list[0]
    return option_list, default_week


def _parse_decision_items(report_text: str) -> list:
    """從決策報告 Markdown 中解析第四節的決策指令表格行。

    容錯處理：
    - 標題格式變體（加粗、不同標點、emoji 前綴等）
    - 若找不到 Section 標題，則在全文搜尋 P1~P99 開頭的 Markdown 表格行
    """
    items = []

    # 嘗試多種 Section 標題格式（抓到下一個同級標題「五」為止）
    section_text = None
    for pat in [
        r'#+\s*\**\s*四[、.．\s]\s*決策指令[與和]資源調度\**[^\n]*(.*?)(?=\n#{1,4}\s+\**\s*五|\Z)',
        r'#+\s*.*決策指令.*資源調度.*\n(.*?)(?=\n#{1,4}\s+\**\s*五|\Z)',
    ]:
        m = re.search(pat, report_text, re.DOTALL)
        if m:
            section_text = m.group(1)
            break

    # Fallback：全文搜尋含 P\d 開頭的表格行
    if section_text is None:
        section_text = report_text

    def _parse_table_row(line: str) -> dict | None:
        """解析一行 Markdown 表格，回傳 dict 或 None。"""
        line = line.strip()
        if not line.startswith('|'):
            return None
        # 跳過分隔線
        if re.match(r'^\|[\s\-:|]+\|?\s*$', line):
            return None
        # 跳過表頭
        if '優先序' in line or '會議議題' in line or '決策原因' in line:
            return None
        cells = [c.strip() for c in line.split('|')]
        cells = [c for c in cells if c]
        # 去除 Markdown 粗體標記 **...** 後再比對 P\d
        first_cell = cells[0].strip('* ') if cells else ''
        if len(cells) >= 2 and re.match(r'P\d', first_cell):
            # 決策指令表（8 欄含決策原因）或舊版 7 欄格式
            if len(cells) >= 8:
                return {
                    'priority': first_cell,
                    'machine': cells[1].strip('* ') if len(cells) > 1 else '',
                    'instruction': (cells[2] if len(cells) > 2 else '').replace('<br>', '\n'),
                    'reason': cells[3] if len(cells) > 3 else '',
                    'lead_dept': cells[4] if len(cells) > 4 else '',
                    'collab_dept': cells[5] if len(cells) > 5 else '',
                    'deadline': cells[6] if len(cells) > 6 else '',
                    'expected': cells[7] if len(cells) > 7 else '',
                }
            else:
                return {
                    'priority': first_cell,
                    'machine': cells[1].strip('* ') if len(cells) > 1 else '',
                    'instruction': (cells[2] if len(cells) > 2 else '').replace('<br>', '\n'),
                    'reason': '',
                    'lead_dept': cells[3] if len(cells) > 3 else '',
                    'collab_dept': cells[4] if len(cells) > 4 else '',
                    'deadline': cells[5] if len(cells) > 5 else '',
                    'expected': cells[6] if len(cells) > 6 else '',
                }
        return None

    for line in section_text.split('\n'):
        parsed = _parse_table_row(line)
        if parsed:
            items.append(parsed)

    # 若 Section 內仍未解析到，則 fallback 全文搜尋
    if not items and section_text is not report_text:
        for line in report_text.split('\n'):
            parsed = _parse_table_row(line)
            if parsed:
                items.append(parsed)

    return items


def _send_exec_agent_chat(query: str, context: str = "", history: list | None = None) -> str:
    """向 Executive Agent 發送對話訊息（不快取，用於即時互動）。"""
    try:
        payload: dict = {"query": query}
        if context:
            payload["context"] = context
        if history:
            payload["history"] = history
        resp = session.post(
            f"{EXEC_AGENT_API_BASE_URL}/chat",
            json=payload,
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("answer") or "").strip() or "Executive Agent 無回傳內容"
    except Exception as e:
        return f"Executive Agent API 呼叫失敗：{str(e)}"


def _save_chat_feedback(week_label: str, query: str, response: str) -> None:
    """將一筆成功的對話輸出附加到 chat_feedback.json，供 Agent 改善參考。"""
    try:
        if os.path.exists(_CHAT_FEEDBACK_FILE):
            with open(_CHAT_FEEDBACK_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
        else:
            records = []
        records.append({
            "saved_at": datetime.now().isoformat(),
            "week_label": week_label,
            "user": query,
            "assistant": response,
        })
        with open(_CHAT_FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 寫入失敗不影響 UI 主流程


def _render_agent_chat_link(agent_name: str, url: str) -> None:
    st.link_button(
        label=f"前往 {agent_name} 對話頁",
        url=url,
        use_container_width=True,
    )


def _get_cached_report_by_view(target_view: str, week_label: str) -> str:
    cached = _get_cached_week_analysis(week_label)
    report_key_map = {
        "ee-report": "ee_report",
        "pe-report": "pe_report",
        "qa-report": "qa_report",
        "exec-report": "exec_report",
    }
    key = report_key_map.get(target_view, "")
    val = cached.get(key) if key else None
    return str(val).strip() if isinstance(val, str) else ""


def _get_cached_trace_by_view(target_view: str, week_label: str) -> dict:
    cached = _get_cached_week_analysis(week_label)
    trace_key_map = {
        "ee-trace": "ee_trace",
        "pe-trace": "pe_trace",
        "qa-trace": "qa_trace",
        "exec-trace": "exec_trace",
    }
    key = trace_key_map.get(target_view, "")
    val = cached.get(key) if key else None
    return val if isinstance(val, dict) else {}


def _render_full_page_report_if_needed(
    target_view: str,
    load_report,
    caption: str,
    render_mode: str = "markdown",
    title_builder=None,
) -> None:
    view_mode = _get_query_param_str("view", "")
    if view_mode != target_view:
        return

    week_label = _get_query_param_str("week", "")
    report = _get_cached_report_by_view(target_view, week_label) or load_report(week_label)
    title = title_builder(week_label) if callable(title_builder) else target_view

    st.title(title)
    st.caption(caption)
    st.markdown(report)
    st.stop()


def _report_title(agent_name: str):
    def _build(week_label: str) -> str:
        return f"{agent_name} 完整報告" if not week_label else f"{agent_name} 完整報告 ({week_label})"

    return _build


def _render_full_page_trace_if_needed(
    target_view: str,
    load_trace,
    caption: str,
    title_builder=None,
    agent_name: str = "Agent",
) -> None:
    view_mode = _get_query_param_str("view", "")
    if view_mode != target_view:
        return

    week_label = _get_query_param_str("week", "")
    trace_data = _get_cached_trace_by_view(target_view, week_label) or load_trace(week_label)
    title = title_builder(week_label) if callable(title_builder) else target_view

    st.title(title)
    st.caption(caption)
    _render_trace_panel(agent_name, trace_data)
    st.stop()


def _render_full_page_chat_if_needed() -> None:
    """當 URL 包含 ?view=exec-chat 時，整頁渲染 Dir Agent 對話頁面。"""
    view_mode = _get_query_param_str("view", "")
    if view_mode != "exec-chat":
        return

    week_label = _get_query_param_str("week", "")
    exec_report = _get_cached_report_by_view("exec-report", week_label) or ""

    title = "Dir Agent 對話" if not week_label else f"Dir Agent 對話 ({week_label})"
    st.title(title)
    st.caption("可向高階主管 Agent 提問、給予回饋或補充資訊，Agent 會結合三部門資訊進行回覆")

    back_col, clear_col = st.columns([8, 1])
    with back_col:
        st.link_button("← 返回 Dashboard", "/", use_container_width=False)
    with clear_col:
        if st.button("🗑️ 清除對話", key="exec_chat_clear", use_container_width=True):
            st.session_state.exec_chat_messages = []
            st.rerun()

    _SUGGESTED_PROMPTS = [
        ("🔍 單機台深度分析", "請針對機台 B1_ETCH_04 進行跨部門深度分析，整合 EE / QA / PE 三部門資訊，給出該機台的風險評級與處置決策"),
        ("⚖️ Cayman vs AMD 機台比對", "請比對 Cayman 與 AMD 使用的機台清單差異，找出可能影響良率的設備差異，並提出限機策略建議"),
        ("📉 產品良率差異歸因", "為什麼 AMD 的良率比 Cayman 差？是不是使用不同機台造成的？請分析並提出限機策略建議。"),
    ]

    if "exec_chat_messages" not in st.session_state:
        st.session_state.exec_chat_messages = []

    if not st.session_state.exec_chat_messages:
        prompt_cols = st.columns(len(_SUGGESTED_PROMPTS))
        for idx, (label, prompt_text) in enumerate(_SUGGESTED_PROMPTS):
            with prompt_cols[idx]:
                if st.button(label, key=f"suggest_prompt_{idx}", use_container_width=True):
                    st.session_state.exec_chat_messages.append({"role": "user", "content": prompt_text})
                    with st.spinner("Dir Agent 回覆中..."):
                        prev_history = st.session_state.exec_chat_messages[:-1]
                        response = _send_exec_agent_chat(prompt_text, context=exec_report, history=prev_history)
                    st.session_state.exec_chat_messages.append({"role": "assistant", "content": response})
                    if not response.startswith("（Agent 未回傳任何內容）"):
                        _save_chat_feedback(week_label, prompt_text, response)
                    st.rerun()

    chat_container = st.container(height=620)
    with chat_container:
        if not st.session_state.exec_chat_messages:
            st.markdown(
                "<div style='text-align:center; color:#888; padding:60px 0;'>"
                "尚無對話紀錄<br>可在下方輸入框提問，或點擊上方建議按鈕快速開始<br>"
                "<br>"
                "「P1 的決策為什麼是處理 B1_DEVP_02」<br>"
                "「P1 的決策可以調整為 48h 內完成嗎？」<br>"
                "「目前有哪些機台已收斂可以移除追蹤？」<br>"
                "「請針對機台 B1_DEVP_02 進行跨部門深度分析，整合 EE / QA / PE 三部門資訊，給出該機台的風險評級與處置決策 ?」<br>"
                "「為什麼 AMD 的良率比 Cayman 差？是不是使用不同機台造成的？請分析並提出限機策略建議」<br>"
                "「請比對 Cayman 與 AMD 使用的機台清單差異，找出可能影響良率的設備差異，並提出限機策略建議」<br>"
                "</div>",
                unsafe_allow_html=True,
            )
        for msg in st.session_state.exec_chat_messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    if user_input := st.chat_input("輸入訊息與 Dir Agent 對話...", key="exec_chat_input"):
        st.session_state.exec_chat_messages.append({"role": "user", "content": user_input})
        with st.spinner("Dir Agent 回覆中..."):
            week_ctx = f"[目前畫面週次：{week_label}]\n\n{exec_report}"
            prev_history = st.session_state.exec_chat_messages[:-1]
            response = _send_exec_agent_chat(user_input, context=week_ctx, history=prev_history)
        st.session_state.exec_chat_messages.append({"role": "assistant", "content": response})
        if not response.startswith("（Agent 未回傳任何內容）"):
            _save_chat_feedback(week_label, user_input, response)
        st.rerun()

    st.stop()


# ── AMD Dashboard 渲染 ──────────────────────────────────────────────
def render_amd_dashboard(weekly, kpi_df):
    weekly = weekly.copy()
    weekly["Week"] = weekly["Week"].apply(_normalize_week_label)
    weekly = weekly.dropna(subset=["Week"]).reset_index(drop=True)
    weekly["week_num"] = weekly["Week"].apply(_parse_week_num)

    if weekly.empty:
        st.warning("AMD 週資料為空，請檢查來源資料 Week/Date 欄位")
        return

    # ── 週次選擇器 ──
    data_weeks = weekly["Week"].tolist()
    week_options, default_week = _build_week_options_with_next(data_weeks)
    if not week_options:
        st.warning("目前無可用週次資料")
        return
    default_idx = week_options.index(default_week) if default_week in week_options else 0

    with st.sidebar:
        st.markdown("### 📅 分析設定")
        selected_week = st.selectbox(
            "選擇分析週次",
            options=week_options,
            index=default_idx,
            key="amd_week_selector",
        )
        force_refresh = st.button("🔄 Rerun", key=f"force_refresh_{selected_week}", use_container_width=True)

    selected_idx = data_weeks.index(selected_week) if selected_week in data_weeks else (len(data_weeks) - 1)
    selected_row = weekly.iloc[selected_idx]
    selected_week_label = str(selected_week)
    avg_particle_yield = weekly["Particle Yield"].mean()

    # 組裝結構化真實良率數據供 PE Agent 程式化產生 Section 1
    prev_week_row = weekly.iloc[selected_idx - 1] if selected_idx >= 1 else None
    yield_data = {
        "week_label": selected_week_label,
        "particle_yield": round(float(selected_row["Particle Yield"]), 2),
        "yield_1p1m": round(float(selected_row["1P1M Yield"]), 2),
        "die_qty": int(selected_row["Die Qty"]),
        "avg_particle_yield": round(float(avg_particle_yield), 2),
    }
    if prev_week_row is not None:
        yield_data["prev_particle_yield"] = round(float(prev_week_row["Particle Yield"]), 2)
        yield_data["prev_week_label"] = str(prev_week_row["Week"])

    analysis_bundle, from_cache = _load_or_build_week_analysis_bundle(
        selected_week_label,
        yield_data=yield_data,
        force_refresh=force_refresh,
    )

    # if from_cache:
    #     refresh_col1.caption("Currently displaying permanent cache results")
    # else:
    #     refresh_col1.caption("Updated and written to permanent cache results")

    ee_report = str(analysis_bundle.get("ee_report") or "EE Agent 無回傳內容")
    pe_report = str(analysis_bundle.get("pe_report") or "PE Agent 無回傳內容")
    qa_report = str(analysis_bundle.get("qa_report") or "QA Agent 無回傳內容")
    exec_report = str(analysis_bundle.get("exec_report") or "Executive Agent 無回傳內容")

    # 提前計算 Action 狀態供上方卡片使用
    _decision_items_early = _parse_decision_items(exec_report)
    _week_decisions_early, _week_actions_early = _load_week_review_state(selected_week_label)
    _approved_early = [k for k, v in _week_decisions_early.items() if v == "approved"]
    _done_early = sum(
        1 for dk in _approved_early
        if str((_week_actions_early.get(dk) or {}).get("action_status") or "") == "已完成"
    )
    _action_total = len(_decision_items_early)
    _completion_rate_early = round(_done_early / len(_approved_early) * 100) if _approved_early else 0
    _rate_color = "#388E3C" if _completion_rate_early >= 80 else ("#F57C00" if _completion_rate_early >= 50 else "#D32F2F")

    # 合併 API KPI 資料到 weekly
    # 使用 week_num 對齊可避免 W20 / 20 / 2026-W20 等格式不一致造成整欄 NA。
    oob_vals = pd.Series([pd.NA] * len(weekly), index=weekly.index, dtype="Float64")
    comp_vals = pd.Series([pd.NA] * len(weekly), index=weekly.index, dtype="Float64")

    if not kpi_df.empty:
        kpi_work = kpi_df.copy()
        if "Week" not in kpi_work.columns:
            if "week" in kpi_work.columns:
                kpi_work["Week"] = kpi_work["week"]
            elif "week_label" in kpi_work.columns:
                kpi_work["Week"] = kpi_work["week_label"]

        kpi_work["Week"] = kpi_work["Week"].apply(_normalize_week_label)
        kpi_work["week_num"] = kpi_work["Week"].apply(_parse_week_num)
        kpi_work = kpi_work.dropna(subset=["week_num"])

        oob_candidates = ["oos_event_count", "oob_count", "oos_count", "event_count"]
        comp_candidates = ["compliance_rate", "pm_compliance", "compliance_pct", "compliance"]
        oob_col = next((c for c in oob_candidates if c in kpi_work.columns), None)
        comp_col = next((c for c in comp_candidates if c in kpi_work.columns), None)

        if oob_col and comp_col:
            kpi_merged = kpi_work[["week_num", oob_col, comp_col]].copy()
            kpi_merged[oob_col] = pd.to_numeric(kpi_merged[oob_col], errors="coerce")
            kpi_merged[comp_col] = pd.to_numeric(kpi_merged[comp_col], errors="coerce")

            comp_non_na = kpi_merged[comp_col].dropna()
            if not comp_non_na.empty and comp_non_na.max() <= 1.5:
                kpi_merged["compliance_pct"] = (kpi_merged[comp_col] * 100).round(1)
            else:
                kpi_merged["compliance_pct"] = kpi_merged[comp_col].round(1)

            kpi_merged = kpi_merged.rename(columns={oob_col: "oos_event_count"})
            kpi_merged = kpi_merged[["week_num", "oos_event_count", "compliance_pct"]]
            kpi_merged = kpi_merged.drop_duplicates(subset=["week_num"], keep="last")

            weekly = weekly.merge(kpi_merged, on="week_num", how="left")
            oob_vals = pd.to_numeric(weekly["oos_event_count"], errors="coerce")
            comp_vals = pd.to_numeric(weekly["compliance_pct"], errors="coerce")

    # 卡片只顯示 API 真實值，缺值顯示 NA
    oob_count = None
    pm_compliance = None
    sel_row = weekly[weekly["Week"].astype(str) == selected_week_label]
    if not sel_row.empty and pd.notna(sel_row.iloc[-1].get("oos_event_count")):
        oob_count = int(sel_row.iloc[-1]["oos_event_count"])
    if not sel_row.empty and pd.notna(sel_row.iloc[-1].get("compliance_pct")):
        pm_compliance = float(sel_row.iloc[-1]["compliance_pct"])

    oob_display = "NA" if oob_count is None else str(oob_count)
    pm_display = "NA" if pm_compliance is None else f"{pm_compliance:.1f}%"

    st.title(f"APX Agent - Y26 {selected_week_label} AMD Weisshorn RDL Analysis")

    # st.markdown("<div style='height:12px;'></div>", unsafe_allow_html=True)

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    with c1:
        render_card("AMD Weisshorn RDL 1P1M Yield", f"{selected_row['1P1M Yield']:.2f}%", "#D32F2F")
    with c2:
        render_card("AMD Weisshorn RDL Particle Yield", f"{selected_row['Particle Yield']:.2f}%", "#D32F2F")
    with c3:
        render_card("OOB Count", oob_display, "#2E7D32")
    with c4:
        render_card("PM Compliance", pm_display, "#2E7D32")
    with c5:
        render_card("決策總數", str(_action_total), "#455A64")
    with c6:
        render_card("處置完成率", f"{_completion_rate_early}%", "#455A64")

    st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    x_labels = weekly["Week"].astype(str).tolist()

    fig.add_trace(
        go.Bar(
            x=x_labels, y=weekly["Die Qty"], name="Die Qty",
            marker_color="lightskyblue", opacity=0.7,
            text=weekly["Die Qty"].astype(int).astype(str),
            textposition="outside", textfont=dict(color="lightskyblue"),
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=x_labels, y=weekly["1P1M Yield"], name="1P1M Yield",
            mode="lines+markers+text",
            text=[f"{v:.2f}%" for v in weekly["1P1M Yield"]],
            textposition="top center", textfont=dict(color="gold"),
            line=dict(color="gold", width=2), marker=dict(color="gold", size=8),
        ),
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=x_labels, y=weekly["Particle Yield"], name="Particle Yield",
            mode="lines+markers+text",
            text=[f"{v:.2f}%" for v in weekly["Particle Yield"]],
            textposition="bottom center", textfont=dict(color="limegreen"),
            line=dict(color="limegreen", width=2), marker=dict(color="limegreen", size=8),
        ),
        secondary_y=True,
    )
    fig.add_hline(
        y=99.5, line_dash="dash", line_color="red",
        annotation_text="Goal 99.5%", annotation_position="top left",
        secondary_y=True,
    )
    fig.update_yaxes(title_text="Die Qty", range=[0, weekly["Die Qty"].max() * 1.5], secondary_y=False)
    fig.update_yaxes(title_text="Yield (%)", range=[80, 102], secondary_y=True)
    fig.update_xaxes(title_text="Week")
    fig.update_layout(
        title="Weekly AMD Weisshorn RDL 1P1M Yield Trend", barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=500,
    )

    # ── OOB Count & PM Compliance 趨勢圖 ──
    fig2 = make_subplots(specs=[[{"secondary_y": True}]])

    fig2.add_trace(
        go.Bar(
            x=x_labels, y=oob_vals, name="OOB Count",
            marker_color="salmon", opacity=0.75,
            text=["NA" if pd.isna(v) else str(int(v)) for v in oob_vals],
            textposition="outside", textfont=dict(color="salmon"),
        ),
        secondary_y=False,
    )
    fig2.add_trace(
        go.Scatter(
            x=x_labels, y=comp_vals, name="PM Compliance",
            mode="lines+markers+text",
            text=["NA" if pd.isna(v) else f"{v:.1f}%" for v in comp_vals],
            textposition="top center", textfont=dict(color="cyan"),
            line=dict(color="cyan", width=2), marker=dict(color="cyan", size=8),
        ),
        secondary_y=True,
    )
    fig2.add_hline(
        y=95, line_dash="dash", line_color="red",
        annotation_text="Goal 95%", annotation_position="top left",
        secondary_y=True,
    )
    valid_oob = oob_vals.dropna()
    oob_max = valid_oob.max() if len(valid_oob) > 0 else 1
    fig2.update_yaxes(title_text="OOB Count", range=[0, max(oob_max * 1.5, 10)], secondary_y=False)
    fig2.update_yaxes(title_text="Compliance (%)", range=[0, 110], secondary_y=True)
    fig2.update_xaxes(title_text="Week")
    fig2.update_layout(
        title="Weekly OOB Count & PM Compliance Trend", barmode="group",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=500,
    )

    chart_col1, chart_col2, chart_col3 = st.columns(3)

    with chart_col1:
        st.plotly_chart(fig, use_container_width=True)

    with chart_col2:
        st.plotly_chart(fig2, use_container_width=True)

    with chart_col3:
        _render_action_trend_chart(_build_action_trend_rows())
    # 上方先顯示 3 個 Agent 觀察（1 row, 3 col）
    a1, a2, a3 = st.columns(3)
    with a1:
        st.markdown("### EE Agent")
        with st.container(height=260):
            st.markdown(ee_report)
        ee_btn1, ee_btn2, ee_btn3 = st.columns(3)
        with ee_btn1:
            st.link_button(" 完整報告", _build_ee_report_link(selected_week_label), use_container_width=True)
        with ee_btn2:
            st.link_button(" Execution Trace", _build_ee_trace_link(selected_week_label), use_container_width=True)
        with ee_btn3:
            st.link_button(" EE Agent Chat", EE_AGENT_CHAT_URL, use_container_width=True)
    with a2:
        st.markdown("### PE Agent")
        with st.container(height=260):
            st.markdown(pe_report)
        pe_btn1, pe_btn2, pe_btn3 = st.columns(3)
        with pe_btn1:
            st.link_button(" 完整報告", _build_pe_report_link(selected_week_label), use_container_width=True)
        with pe_btn2:
            st.link_button(" Execution Trace", _build_pe_trace_link(selected_week_label), use_container_width=True)
        with pe_btn3:
            st.link_button(" PE Agent Chat", PE_AGENT_CHAT_URL, use_container_width=True)
    with a3:
        st.markdown("### QA Agent")
        with st.container(height=260):
            st.markdown(qa_report)
        qa_btn1, qa_btn2, qa_btn3 = st.columns(3)
        with qa_btn1:
            st.link_button(" 完整報告", _build_qa_report_link(selected_week_label), use_container_width=True)
        with qa_btn2:
            st.link_button(" Execution Trace", _build_qa_trace_link(selected_week_label), use_container_width=True)
        with qa_btn3:
            st.link_button(" QA Agent Chat", QA_AGENT_CHAT_URL, use_container_width=True)

    # ── 高階主管整合決策區塊 ──
    st.markdown("---")
    st.markdown("###  Dir Agent")
    st.caption("統整 EE / QA / PE 三部門分析報告，產出跨部門風險分級與資源調度決策")

    with st.container(height=500):
        st.markdown(exec_report)
    dir_btn1, dir_btn2, dir_btn3 = st.columns(3)
    with dir_btn1:
        st.link_button(" 完整決策報告", _build_exec_report_link(selected_week_label), use_container_width=True)
    with dir_btn2:
        st.link_button(" Execution Trace", _build_exec_trace_link(selected_week_label), use_container_width=True)
    with dir_btn3:
        st.link_button("💬 Dir Agent 對話", _build_exec_chat_link(selected_week_label), use_container_width=True)

    # ── 決策審核區塊 ──
    st.markdown("### ✅ 決策審核")
    st.caption("審核以下決策項目，核准後可連結至機台保養系統執行")

    decision_items = _parse_decision_items(exec_report)
    week_decisions, week_actions = _load_week_review_state(selected_week_label)

    if not decision_items:
        st.info("報告中尚未解析到決策項目。")
    else:
        _render_action_completion_block(decision_items, week_decisions, week_actions)

        # 依 priority 分組，同一優先級排在同一列
        priority_groups: dict[str, list] = {}
        for item in decision_items:
            priority_groups.setdefault(item["priority"], []).append(item)

        for priority_label in sorted(priority_groups.keys()):
            group_items = priority_groups[priority_label]
            st.markdown(f"#### {priority_label}")
            cols = st.columns(len(group_items))
            for col, item in zip(cols, group_items):
                dkey = f"{item['priority']}_{item['machine']}"
                dstatus = week_decisions.get(dkey, "pending")

                if dstatus == "approved":
                    status_label = "✅ 簽修機"
                    card_border_color = "#388E3C"
                elif dstatus == "on_hold":
                    status_label = "⏸️ 暫緩"
                    card_border_color = "#7B1FA2"
                else:
                    status_label = "⏳ 待審核"
                    card_border_color = "#455A64"

                with col:
                    with st.container(border=True):
                        st.markdown(
                            f"**{item['machine']}** {status_label}  \n"
                            f"主責：{item['lead_dept']}（協作：{item['collab_dept']}）  \n"
                            f"時限：{item['deadline']}"
                        )

                        btn1, btn2, btn3 = st.columns(3)
                        with btn1:
                            if st.button("🔗 指派", key=f"assign_{dkey}", use_container_width=True,
                                         disabled=(dstatus == "approved")):
                                st.info(f"已指派 {item['machine']} 給 {item['lead_dept']}，請前往機台保養系統執行")
                                _update_week_decision_status(selected_week_label, dkey, "approved")
                                _remove_deferred_decision(selected_week_label, item["machine"])
                                st.rerun()
                        with btn2:
                            if st.button("✅ 簽修機", key=f"approve_{dkey}", use_container_width=True,
                                         disabled=(dstatus == "approved")):
                                _update_week_decision_status(selected_week_label, dkey, "approved")
                                _remove_deferred_decision(selected_week_label, item["machine"])
                                st.rerun()
                        with btn3:
                            if st.button("⏸️ 暫緩", key=f"hold_{dkey}", use_container_width=True,
                                         disabled=(dstatus == "on_hold")):
                                _update_week_decision_status(selected_week_label, dkey, "on_hold")
                                _record_deferred_decision(selected_week_label, item)
                                st.rerun()

                        # ── 重複暫緩提醒 ──
                        prev_deferrals = _get_previous_deferrals(selected_week_label, item["machine"])
                        if prev_deferrals:
                            deferred_weeks = ", ".join(r["week"] for r in prev_deferrals)
                            st.warning(
                                f"⚠️ **{item['machine']}** 曾於 **{deferred_weeks}** 被暫緩，"
                                f"累計 **{len(prev_deferrals)}** 次"
                            )

                        with st.expander("📝 詳細內容", expanded=False):
                            if item.get("reason"):
                                st.markdown(f"**決策原因：** {item['reason']}")
                            st.markdown(f"**決策指令：**\n\n{item['instruction']}")
                            st.markdown(f"**預期效果：** {item['expected']}")

                        with st.expander("🛠️ 處置回報", expanded=False):
                            current_action = week_actions.get(dkey, {})
                            if not isinstance(current_action, dict):
                                current_action = {}

                            action_status_options = ["未開始", "進行中", "已完成", "阻塞"]
                            current_action_status = str(current_action.get("action_status") or "未開始")
                            if current_action_status not in action_status_options:
                                action_status_options.append(current_action_status)
                            current_action_status_idx = action_status_options.index(current_action_status)

                            action_owner = st.text_input(
                                "負責人",
                                value=str(current_action.get("owner") or item.get("lead_dept") or ""),
                                key=f"action_owner_{selected_week_label}_{dkey}",
                            )
                            action_status = st.selectbox(
                                "處置進度",
                                options=action_status_options,
                                index=current_action_status_idx,
                                key=f"action_status_{selected_week_label}_{dkey}",
                            )
                            action_notes = st.text_area(
                                "已執行處置",
                                value=str(current_action.get("notes") or ""),
                                key=f"action_notes_{selected_week_label}_{dkey}",
                                height=100,
                                placeholder="例如：已完成腔體清潔與參數校正",
                            )
                            _start_val = current_action.get("start_time") or ""
                            _start_date = None
                            if _start_val:
                                try:
                                    _start_date = datetime.strptime(_start_val, "%Y-%m-%d").date()
                                except Exception:
                                    _start_date = None
                            action_start_time = st.date_input(
                                "處置開始時間",
                                value=_start_date,
                                key=f"action_start_{selected_week_label}_{dkey}",
                            )

                            _end_val = current_action.get("end_time") or ""
                            _end_date = None
                            if _end_val:
                                try:
                                    _end_date = datetime.strptime(_end_val, "%Y-%m-%d").date()
                                except Exception:
                                    _end_date = None
                            action_end_time = st.date_input(
                                "處置結束時間",
                                value=_end_date,
                                key=f"action_end_{selected_week_label}_{dkey}",
                            )

                            action_updated_at = str(current_action.get("updated_at") or "")
                            if action_updated_at:
                                st.caption(f"上次更新：{action_updated_at}")

                            save_col, clear_col = st.columns(2)
                            with save_col:
                                if st.button("💾 儲存", key=f"save_action_{selected_week_label}_{dkey}", use_container_width=True):
                                    _save_week_action_item(
                                        selected_week_label,
                                        dkey,
                                        {
                                            "priority": item.get("priority", ""),
                                            "machine": item.get("machine", ""),
                                            "owner": action_owner.strip(),
                                            "action_status": action_status,
                                            "notes": action_notes.strip(),
                                            "start_time": action_start_time.strftime("%Y-%m-%d") if action_start_time else "",
                                            "end_time": action_end_time.strftime("%Y-%m-%d") if action_end_time else "",
                                            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                        },
                                    )
                                    st.success("已儲存")
                                    st.rerun()
                            with clear_col:
                                if st.button("🗑️ 清除", key=f"clear_action_{selected_week_label}_{dkey}", use_container_width=True):
                                    _, actions_map = _load_week_review_state(selected_week_label)
                                    if dkey in actions_map:
                                        del actions_map[dkey]
                                        _save_cached_week_actions(selected_week_label, actions_map)
                                        st.rerun()




#
# ── 主程式 ───────────────────────────────────────────────────────────
_render_full_page_report_if_needed(
    "ee-report",
    load_ee_latest_week_report,
    "資料來源：/latest-week",
    render_mode="info",
    title_builder=_report_title("EE Agent"),
)
_render_full_page_report_if_needed(
    "pe-report",
    load_pe_latest_week_report,
    "資料來源：/api/weekly-report",
    render_mode="info",
    title_builder=_report_title("PE Agent"),
)
_render_full_page_report_if_needed(
    "qa-report",
    load_qa_latest_week_report,
    "資料來源：QA Agent API /generate-report",
    title_builder=_report_title("QA Agent"),
)
_render_full_page_report_if_needed(
    "exec-report",
    load_executive_decision_report,
    "資料來源：Executive Agent API /api/weekly-decision-fast（統整 EE / QA / PE 三部門）",
    title_builder=lambda _week_label: "Dir Agent 整合決策報告",
)
_render_full_page_trace_if_needed(
    "ee-trace",
    load_ee_latest_week_trace,
    "資料來源：/latest-week（include_trace=true）",
    title_builder=_report_title("EE Agent Execution Trace"),
    agent_name="EE Agent",
)
_render_full_page_trace_if_needed(
    "pe-trace",
    load_pe_latest_week_trace,
    "資料來源：PE Agent API /api/weekly-report（include_trace=true）",
    title_builder=_report_title("PE Agent Execution Trace"),
    agent_name="PE Agent",
)
_render_full_page_trace_if_needed(
    "qa-trace",
    load_qa_latest_week_trace,
    "資料來源：QA Agent API /generate-report（include_trace=true）",
    title_builder=_report_title("QA Agent Execution Trace"),
    agent_name="QA Agent",
)
_render_full_page_trace_if_needed(
    "exec-trace",
    load_executive_decision_trace,
    "資料來源：Executive Agent API /api/weekly-decision-fast（include_trace=true）",
    title_builder=_report_title("Dir Agent Execution Trace"),
    agent_name="Dir Agent",
)
_render_full_page_chat_if_needed()
_weekly_refresh_key = _current_iso_week_key()
render_amd_dashboard(load_amd_data(_weekly_refresh_key), load_kpi_data(_weekly_refresh_key))