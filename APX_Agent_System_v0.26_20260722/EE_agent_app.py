"""
EE_Agent_app.py
-----------------------
基於 OpenAI Agent SDK 的 EE (設備工程師) AI Agent

資料來源：本地 Excel（每週熱點機台、Rework Rate）
"""

import os
import json
import re
import asyncio
from datetime import datetime
from typing import Optional
import httpx
import requests
import pandas as pd
import streamlit as st
import nest_asyncio
from agents import Agent, Runner, function_tool, OpenAIResponsesModel, set_tracing_disabled, AsyncOpenAI

session = requests.Session()
session.trust_env = False  # 忽略環境變數（包含 proxy）

#  解決 Streamlit 與 asyncio 的嵌套迴圈問題 
try:
    nest_asyncio.apply()
except Exception:
    pass


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_PAGE_ICON_PATH = os.path.join(_BASE_DIR, "assets", "APX_Agent_icon.png")
_LOG_DIR = os.path.join(_BASE_DIR, "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "ee_agent_debug.log")

#  全域設定 
API_BASE_URL  = "http://10.11.33.5:9120/v1"
API_MODEL     = "/model/gpt-oss-120b"

REWORK_WEEKLY_EXCEL_PATH = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_rework_output\weekly_rework_report_(Security C).xlsx"
REWORK_WEEKLY_SHEET_NAME = "weekly_summary"
REWORK_LAYERS = ["PR1", "PR2", "uPad"]
WEEKLY_MACHINE_EXCEL_PATH = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_report_top5_machine_(Security C).xlsx"
RW_MACHINE_CONCENTRATION_PATH = (
    r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_rework_top5\Merged - 機台統計詳細資料_(Security C).xlsx"
)

EMBEDDING_API = "http://10.11.32.244:18089"
QDRANT_API = "http://10.10.19.35:6333"
COLLECTION = "BP_APX_Particle_Meeting_Reports"
DENSE_VEC = "Dense-vector"
SPARSE_VEC = "Sparse-vector"
USE_SYSTEM_PROXY = False
REQUESTS_PROXIES = None if USE_SYSTEM_PROXY else {"http": None, "https": None}

set_tracing_disabled(True)


def _append_local_log(title: str, payload: str) -> None:
    """將除錯資訊寫入地端 UTF-8 log，避免 console 編碼限制。"""
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{ts}] {title}\n")
            f.write(payload)
            if not payload.endswith("\n"):
                f.write("\n")
            f.write("-" * 80 + "\n")
    except Exception:
        # logging 不應影響主流程
        pass


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _request_kwargs(timeout: int = 60) -> dict:
    kwargs = {"timeout": timeout}
    if REQUESTS_PROXIES is not None:
        kwargs["proxies"] = REQUESTS_PROXIES
    return kwargs


def _extract_week_value(value: str) -> str:
    text = str(value or "")
    weeks = re.findall(r"\d{4}-W\d{1,2}", text)
    if not weeks:
        return ""
    year, week = weeks[-1].split("-W")
    return f"{year}-W{int(week):02d}"


def _week_sort_key(week_text: str) -> tuple:
    week = _extract_week_value(week_text)
    if not week:
        return (0, 0)
    year, wk = week.split("-W")
    return (int(year), int(wk))


def _normalize_machine_token(value: str) -> str:
    """將機台字串標準化為可比對 token（保留英數與底線）。"""
    text = re.sub(r"[^A-Z0-9_]+", "_", str(value or "").upper())
    return re.sub(r"_+", "_", text).strip("_")


def _extract_machine_family(value: str) -> str:
    """從機台代號抽出機型族群（例如 B1_SPUT_03 -> SPUT）。"""
    token = _normalize_machine_token(value)
    if not token:
        return ""
    parts = [p for p in token.split("_") if p]
    if len(parts) >= 3:
        return parts[1]
    if len(parts) >= 2:
        # 若最後一段為純數字，視為序號，回傳中段機型。
        if parts[-1].isdigit():
            return parts[-2]
    return parts[0] if parts else ""


def _load_weekly_machine_hotspots() -> pd.DataFrame:
    """讀取每週熱點機台資料，並標準化欄位型態。"""
    df = pd.read_excel(WEEKLY_MACHINE_EXCEL_PATH, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = {"Machine_ID", "Week", "Weighted_Risk_index"}
    missing_cols = sorted(required_cols - set(df.columns))
    if missing_cols:
        raise ValueError(f"weekly hotspot Excel 缺少必要欄位: {', '.join(missing_cols)}")

    df["Machine_ID"] = df["Machine_ID"].astype(str).str.strip()
    df["Week"] = df["Week"].apply(_extract_week_value)
    df = df[(df["Machine_ID"] != "") & (df["Week"] != "")].copy()

    numeric_cols = [
        "Avg_OOC_Rate",
        "avg_PI_value",
        "Particle_yield_loss_Mean",
        "Particle_yield_loss_sum",
        "Weighted_Risk_index",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    return df


def _resolve_target_week(week_range: str, weekly_df: pd.DataFrame) -> str:
    explicit_week = _extract_week_value(week_range)
    available_weeks = sorted(weekly_df["Week"].unique(), key=_week_sort_key)
    if not available_weeks:
        raise ValueError("weekly hotspot Excel 無有效週次資料")

    if explicit_week and explicit_week in set(available_weeks):
        return explicit_week

    if week_range and "Week_Range" in weekly_df.columns:
        matched_df = weekly_df[weekly_df["Week_Range"].astype(str).str.strip() == week_range.strip()]
        if not matched_df.empty:
            matched_weeks = sorted(matched_df["Week"].unique(), key=_week_sort_key)
            return matched_weeks[-1]

    return available_weeks[-1]


def _normalize_week_token(week_range: str) -> str:
    token = str(week_range or "").strip().upper()
    if not token:
        return ""

    if "-W" in token:
        token = "W" + token.split("-W")[-1]
    elif token.startswith("W"):
        token = "W" + token[1:]
    else:
        token = "W" + token

    digits = "".join(ch for ch in token if ch.isdigit())
    if not digits:
        return ""
    return f"W{int(digits):02d}"


def _load_rw_machine_concentration() -> pd.DataFrame:
    """讀取 Rework 機台集中性原始資料。"""
    if not os.path.exists(RW_MACHINE_CONCENTRATION_PATH):
        raise FileNotFoundError(f"找不到 Rework 機台集中性檔案：{RW_MACHINE_CONCENTRATION_PATH}")

    df = pd.read_excel(RW_MACHINE_CONCENTRATION_PATH, engine="openpyxl")
    if df.empty:
        return df

    required_cols = [
        "Machine_ID",
        "LOT_Type",
        "Week",
        "RW_Weighted_Risk_Index",
        "Avg_OOC_Rate",
        "avg_PI_value",
        "Wafer_Qty",
        "Avg_Rework_Ratio(%)",
        "Stat_End_Date",
    ]
    for col in required_cols:
        if col not in df.columns:
            df[col] = None

    df["Week"] = df["Week"].astype(str).str.strip().str.upper()
    df["LOT_Type"] = df["LOT_Type"].astype(str).str.strip()
    for col in ["RW_Weighted_Risk_Index", "Avg_OOC_Rate", "avg_PI_value", "Wafer_Qty", "Avg_Rework_Ratio(%)"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    df["Stat_End_Date"] = pd.to_datetime(df["Stat_End_Date"].astype(str), format="%Y%m%d", errors="coerce")
    return df


def _resolve_rw_target_week(week_range: str, rw_df: pd.DataFrame) -> str:
    requested = _normalize_week_token(week_range)
    if requested:
        return requested

    dated = rw_df.dropna(subset=["Stat_End_Date"]).copy()
    if not dated.empty:
        latest_date = dated["Stat_End_Date"].max()
        latest_rows = dated[dated["Stat_End_Date"] == latest_date]
        if not latest_rows.empty:
            return str(latest_rows["Week"].iloc[0]).strip().upper()

    week_num = pd.to_numeric(rw_df["Week"].astype(str).str.extract(r"(\d+)")[0], errors="coerce")
    if week_num.notna().any():
        return f"W{int(week_num.max()):02d}"
    return ""


def _build_rework_layer_trend_rows(df: pd.DataFrame, layers: list, trend_weeks: int = 6) -> list:
    """輸出各 Layer 最近幾週趨勢資料，供週報顯示。"""
    result = []
    if df.empty:
        return result

    all_weeks = sorted(df["year_week"].unique(), key=_week_sort_key)
    recent_weeks = set(all_weeks[-trend_weeks:])

    for layer in layers:
        layer_df = df[(df["Layer"] == layer) & (df["year_week"].isin(recent_weeks))].copy()
        if layer_df.empty:
            continue
        layer_df = layer_df.sort_values("year_week", key=lambda s: s.map(_week_sort_key))
        result.append(
            {
                "layer": layer,
                "rows": [
                    {
                        "year_week": r["year_week"],
                        "rework_rate_pct": round(_safe_float(r.get("rework_rate_pct", 0)), 2),
                        "wow_diff_pct": round(_safe_float(r.get("wow_diff_pct", 0)), 2),
                        "latest_4w_avg_rate_pct": round(_safe_float(r.get("latest_4w_avg_rate_pct", 0)), 2),
                        "this_week_vs_latest_4w_avg_diff_pct": round(
                            _safe_float(r.get("this_week_vs_latest_4w_avg_diff_pct", 0)), 2
                        ),
                    }
                    for _, r in layer_df.iterrows()
                ],
            }
        )
    return result


def _embed_query_dense(text: str) -> list[float]:
    resp = requests.post(
        f"{EMBEDDING_API}/embed",
        json={"inputs": [text]},
        headers={"Content-Type": "application/json"},
        **_request_kwargs(timeout=30),
    )
    resp.raise_for_status()
    return resp.json()[0]


def _embed_query_sparse(text: str) -> Optional[dict]:
    try:
        resp = requests.post(
            f"{EMBEDDING_API}/embed_sparse",
            json={"inputs": [text]},
            headers={"Content-Type": "application/json"},
            **_request_kwargs(timeout=30),
        )
        resp.raise_for_status()
        tokens = resp.json()[0]
        indices, values = [], []
        for token in tokens:
            if isinstance(token, dict):
                indices.append(int(token["index"]))
                values.append(float(token["value"]))
            else:
                indices.append(int(token[0]))
                values.append(float(token[1]))
        return {"indices": indices, "values": values}
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 424:
            return None
        raise


def _search_knowledge_hits(
    query_text: str,
    top_k: int = 5,
    agent_type: Optional[str] = None,
    equipment_id: Optional[str] = None,
    meeting_week: Optional[str] = None,
    use_sparse: bool = False,
) -> list[dict]:
    must_conditions = []
    if agent_type:
        must_conditions.append({
            "key": "agent_type",
            "match": {"value": str(agent_type).upper()},
        })
    if equipment_id:
        must_conditions.append({
            "should": [
                {"key": "equipment_id", "match": {"text": str(equipment_id)}},
                {"key": "machine", "match": {"text": str(equipment_id)}},
            ]
        })
    if meeting_week:
        must_conditions.append({
            "key": "meeting_week",
            "match": {"value": str(meeting_week).upper()},
        })
    qdrant_filter = {"must": must_conditions} if must_conditions else None

    dense_vec = _embed_query_dense(query_text)
    sparse_vec = _embed_query_sparse(query_text) if use_sparse else None

    if sparse_vec:
        body = {
            "prefetch": [
                {"query": dense_vec, "using": DENSE_VEC, "limit": top_k * 3},
                {
                    "query": {"indices": sparse_vec["indices"], "values": sparse_vec["values"]},
                    "using": SPARSE_VEC,
                    "limit": top_k * 3,
                },
            ],
            "query": {"fusion": "rrf"},
            "limit": top_k,
            "with_payload": True,
        }
        if qdrant_filter:
            body["filter"] = qdrant_filter
        url = f"{QDRANT_API}/collections/{COLLECTION}/points/query"
    else:
        body = {
            "vector": {"name": DENSE_VEC, "vector": dense_vec},
            "limit": top_k,
            "with_payload": True,
        }
        if qdrant_filter:
            body["filter"] = qdrant_filter
        url = f"{QDRANT_API}/collections/{COLLECTION}/points/search"

    resp = requests.post(
        url,
        json=body,
        headers={"Content-Type": "application/json"},
        **_request_kwargs(timeout=60),
    )
    resp.raise_for_status()
    data = resp.json()
    raw = data.get("result", {})
    if isinstance(raw, dict):
        return raw.get("points", [])
    return raw


@function_tool
def search_cip_success_cases(
    machine_id: str = "",
    week_range: str = "",
    query_text: str = "particle contamination 改善 CIP 成功 案例",
    top_k: int = 5,
    agent_type: str = "",
    use_sparse: bool = False,
) -> str:
    """
    查詢歷史 Particle CIP 知識庫，僅回傳同機台可參考的成功案例。

    參數：
    - machine_id : 機台代號（例如 B1_ETCH_04），會同時比對 equipment_id 與 machine 欄位
    - week_range : 週次（選填）；預設不強制過濾，避免漏掉歷史同機台案例
    - query_text : 語意查詢文字
    - top_k : 回傳案例數
    - agent_type : EE / PE / 空字串
    - use_sparse : 是否啟用 Hybrid 搜尋（若 embedding 模型不支援，會自動退回 Dense）
    """
    return json.dumps(
        _search_cip_core(
            machine_id=machine_id,
            week_range=week_range,
            query_text=query_text,
            top_k=top_k,
            agent_type=agent_type,
            use_sparse=use_sparse,
        ),
        ensure_ascii=False,
        indent=2,
    )


def _search_cip_core(
    machine_id: str,
    week_range: str = "",
    query_text: str = "particle contamination 改善 CIP 成功 案例",
    top_k: int = 5,
    agent_type: str = "",
    use_sparse: bool = False,
) -> dict:
    """
    查詢歷史 Particle CIP 知識庫，僅回傳同機台可參考的成功案例。

    參數：
    - machine_id : 機台代號（例如 B1_ETCH_04），會同時比對 equipment_id 與 machine 欄位
    - week_range : 週次（選填）；預設不強制過濾，避免漏掉歷史同機台案例
    - query_text : 語意查詢文字
    - top_k : 回傳案例數
    - agent_type : EE / PE / 空字串
    - use_sparse : 是否啟用 Hybrid 搜尋（若 embedding 模型不支援，會自動退回 Dense）
    """
    normalized_week = _extract_week_value(week_range)
    if not normalized_week and week_range:
        week_range_upper = str(week_range).strip().upper()
        normalized_week = week_range_upper if week_range_upper.startswith("W") else ""

    query_machine_token = _normalize_machine_token(machine_id)
    query_machine_family = _extract_machine_family(machine_id)

    # 第一次：依語意 + 機台條件召回
    hits = _search_knowledge_hits(
        query_text=query_text,
        top_k=max(int(top_k), 1) * 4,
        agent_type=(agent_type or None),
        equipment_id=(machine_id or None),
        meeting_week=None,
        use_sparse=bool(use_sparse),
    )

    # 第二次 fallback：若召回太少，放寬 Qdrant filter 但最後仍由本地邏輯嚴格保留同機台
    if machine_id and len(hits) < max(int(top_k), 1):
        hits = _search_knowledge_hits(
            query_text=f"{machine_id} {query_text}",
            top_k=max(int(top_k), 1) * 8,
            agent_type=(agent_type or None),
            equipment_id=None,
            meeting_week=None,
            use_sparse=bool(use_sparse),
        )

    normalized_machine = str(machine_id or "").strip().upper()
    cases = []
    for hit in hits:
        payload = hit.get("payload", {}) or {}
        src_agent = str(payload.get("agent_type", "")).upper()
        ee_mid = str(payload.get("equipment_id", "")).strip().upper()
        pe_mid = str(payload.get("machine", "")).strip().upper()

        ee_token = _normalize_machine_token(ee_mid)
        pe_token = _normalize_machine_token(pe_mid)
        ee_family = _extract_machine_family(ee_mid)
        pe_family = _extract_machine_family(pe_mid)

        exact_machine_match = bool(
            normalized_machine
            and normalized_machine in {ee_mid, pe_mid}
        )
        family_machine_match = bool(
            query_machine_family
            and query_machine_family in {ee_family, pe_family}
        )
        token_partial_match = bool(
            query_machine_token
            and (
                query_machine_token in ee_token
                or query_machine_token in pe_token
                or ee_token in query_machine_token
                or pe_token in query_machine_token
            )
        )

        # 指定 machine_id 時，允許：同機台 > 同機型族群 > token 局部匹配。
        if normalized_machine and not (exact_machine_match or family_machine_match or token_partial_match):
            continue

        match_level = (
            "exact" if exact_machine_match
            else "family" if family_machine_match
            else "partial" if token_partial_match
            else "semantic"
        )

        if src_agent == "EE":
            cip_actions = [a.strip() for a in str(payload.get("action_plan_cip", "")).split("|") if a.strip()]
            cases.append({
                "score": round(_safe_float(hit.get("score", 0.0)), 4),
                "source_agent": "EE",
                "meeting_week": payload.get("meeting_week", ""),
                "machine": payload.get("equipment_id", ""),
                "issue_description": payload.get("issue_description", ""),
                "root_cause_analysis": payload.get("root_cause_analysis", ""),
                "cip_actions": cip_actions,
                "effectiveness_kpi": payload.get("effectiveness_kpi", ""),
                "match_level": match_level,
            })
        else:
            cases.append({
                "score": round(_safe_float(hit.get("score", 0.0)), 4),
                "source_agent": src_agent or "PE",
                "meeting_week": payload.get("meeting_week", ""),
                "machine": payload.get("machine", ""),
                "process_step": payload.get("process_step", ""),
                "defect_mode": payload.get("defect_mode", ""),
                "mechanism": payload.get("mechanism", ""),
                "cip_action": payload.get("cip_action", ""),
                "monitor_item": payload.get("monitor_item", ""),
                "yield_impact": payload.get("yield_impact", ""),
                "match_level": match_level,
            })

    level_rank = {"exact": 0, "family": 1, "partial": 2, "semantic": 3}
    cases = sorted(
        cases,
        key=lambda x: (level_rank.get(x.get("match_level", "semantic"), 9), -x.get("score", 0)),
    )[:max(int(top_k), 1)]
    for idx, c in enumerate(cases, 1):
        c["rank"] = idx

    output = {
        "query": {
            "machine_id": machine_id,
            "week_range": normalized_week or week_range,
            "query_text": query_text,
            "top_k": max(int(top_k), 1),
            "agent_type": (agent_type or "ALL").upper(),
            "use_sparse": bool(use_sparse),
        },
        "case_count": len(cases),
        "cases": cases,
        "guidance": (
            "僅可使用同機台案例作為 Action Item 參考；"
            "若查無同機台案例，請明確標註『本週無同機台可引用 CIP 成功案例』。"
        ),
    }
    return output


@function_tool
def search_cip_cases_batch(
    machine_ids: str,
    week_range: str = "",
    query_text: str = "particle contamination 改善 CIP 成功 案例",
    top_k: int = 3,
    agent_type: str = "EE",
    use_sparse: bool = False,
) -> str:
    """一次查詢多台機台的 CIP 成功案例。machine_ids 以逗號分隔。"""
    try:
        ids = [m.strip() for m in str(machine_ids).split(",") if m.strip()]
        if not ids:
            return json.dumps(
                {
                    "machine_ids": [],
                    "count": 0,
                    "results": {},
                    "message": "machine_ids 為空，請提供逗號分隔的機台代號。",
                },
                ensure_ascii=False,
                indent=2,
            )

        results = {
            mid: _search_cip_core(
                machine_id=mid,
                week_range=week_range,
                query_text=query_text,
                top_k=top_k,
                agent_type=agent_type,
                use_sparse=use_sparse,
            )
            for mid in ids
        }
        return json.dumps(
            {
                "machine_ids": ids,
                "count": len(ids),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    except Exception as e:
        return f"批次查詢 CIP 成功案例時發生錯誤：{str(e)}"


#  EE Agent 工具定義 (@function_tool) 

# ========== Rework Rate 資料讀取 ==========

def _load_rework_weekly_summary() -> pd.DataFrame:
    """
    讀取已週聚合的 Rework Excel（weekly_summary），
    並回傳後續分析需要的欄位。
    """
    df = pd.read_excel(
        REWORK_WEEKLY_EXCEL_PATH,
        sheet_name=REWORK_WEEKLY_SHEET_NAME,
        engine="openpyxl",
    )
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = [
        "year_week",
        "Layer",
        "total_wafers",
        "particle_rework",
        "rework_rate_pct",
        "latest_week_rate_pct",
        "previous_week_rate_pct",
        "wow_diff_pct",
        "latest_4w_avg_rate_pct",
        "this_week_vs_latest_4w_avg_diff_pct",
    ]
    missing = [col for col in required_cols if col not in df.columns]
    if missing:
        raise ValueError(f"Rework weekly Excel 缺少必要欄位: {missing}")

    df["Layer"] = df["Layer"].astype(str).str.strip()
    df["year_week"] = df["year_week"].astype(str).apply(_extract_week_value)
    df = df[(df["Layer"].isin(REWORK_LAYERS)) & (df["year_week"] != "")].copy()

    numeric_cols = [
        "total_wafers",
        "particle_rework",
        "rework_rate_pct",
        "latest_week_rate_pct",
        "previous_week_rate_pct",
        "wow_diff_pct",
        "latest_4w_avg_rate_pct",
        "this_week_vs_latest_4w_avg_diff_pct",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    df = df.drop_duplicates(subset=["year_week", "Layer"], keep="last")
    df = df.sort_values(["year_week", "Layer"], key=lambda s: s.map(_week_sort_key) if s.name == "year_week" else s)
    return df.reset_index(drop=True)


def _format_rework_summary(df: pd.DataFrame) -> str:
    if df.empty:
        return "（無 Rework 資料）"
    lines = []
    for week, wdf in df.groupby("year_week", sort=True):
        total_wafers = wdf["total_wafers"].sum()
        total_particle = wdf["particle_rework"].sum()
        overall_rate = round(total_particle / max(total_wafers, 1) * 100, 2)
        lines.append(f"**{week}**  整體 Rework rate (particle): {overall_rate}%  "
                     f"(particle={total_particle}, wafers={total_wafers})")
        for _, row in wdf.iterrows():
            lines.append(
                f"  - {row['Layer']}: rework rate={row['rework_rate_pct']}%  "
                f"(particle={row['particle_rework']}, wafers={row['total_wafers']})"
            )
    return "\n".join(lines)


@function_tool
def get_rework_weekly_data(week_range: str = "", lookback_weeks: int = 4, trend_weeks: int = 6) -> str:
    """
    取得 Rework Rate 原始數據（JSON），固定統計 Layer = PR1 / PR2 / uPad，指標為 particle。
    回傳包含各週明細、WoW 差異、近 4 週平均趨勢資料。

    參數：
    - week_range     : 指定目標週次，例如 "2026-W12"；未指定則使用資料中最新週
    - lookback_weeks : 從目標週往前回看週數（含目標週），預設 4
    - trend_weeks    : layer_trends 顯示最近趨勢週數，預設 6
    """
    try:
        rw_df = _load_rework_weekly_summary()
        if rw_df.empty:
            output = {
                "week_range": _extract_week_value(week_range) or "",
                "weeks_analyzed": [],
                "detail": "無 Rework 資料",
                "layer_trends": [],
            }
            return json.dumps(output, ensure_ascii=False, indent=2)

        all_rw_weeks = sorted(rw_df["year_week"].unique(), key=_week_sort_key)
        target_week = _extract_week_value(week_range) if week_range else ""
        if not target_week or target_week not in set(all_rw_weeks):
            target_week = all_rw_weeks[-1]

        valid_weeks = [w for w in all_rw_weeks if _week_sort_key(w) <= _week_sort_key(target_week)]
        lookback_weeks = max(int(lookback_weeks), 1)
        trend_weeks = max(int(trend_weeks), 1)

        recent_weeks = valid_weeks[-lookback_weeks:] if len(valid_weeks) >= lookback_weeks else valid_weeks
        rw_recent = rw_df[rw_df["year_week"].isin(recent_weeks)]

        output = {
            "week_range": target_week,
            "weeks_analyzed": list(recent_weeks),
            "detail": _format_rework_summary(rw_recent),
            "layer_trends": _build_rework_layer_trend_rows(
                rw_df[rw_df["year_week"].isin(valid_weeks)],
                REWORK_LAYERS,
                trend_weeks=trend_weeks,
            ),
        }
        return json.dumps(output, ensure_ascii=False, indent=2)
    except FileNotFoundError:
        return f"找不到 Rework Excel 檔案：{REWORK_WEEKLY_EXCEL_PATH}"
    except Exception as e:
        return f"取得 Rework Rate 資料時發生錯誤：{str(e)}"


@function_tool
def get_recurring_abnormal_machines() -> str:
    """
    取得長期重複異常的高關注機台清單。
    依出現週次數由多到少排序，包含出現次數與趨勢判定（上升/下降/持平）。
    """
    try:
        df = _load_weekly_machine_hotspots()
        if df.empty:
            return "目前無重複異常機台資料。"
        all_weeks = sorted(df["Week"].unique(), key=_week_sort_key)
        latest_week = all_weeks[-1]
        occurrence = df.groupby("Machine_ID")["Week"].nunique().rename("occurrence_count")
        avg_risk = df.groupby("Machine_ID")["Weighted_Risk_index"].mean().rename("avg_risk")
        latest_df = df[df["Week"] == latest_week][["Machine_ID", "Weighted_Risk_index"]].set_index("Machine_ID")
        summary = pd.concat([occurrence, avg_risk], axis=1)
        summary = summary[summary["occurrence_count"] >= 2].sort_values("occurrence_count", ascending=False)
        if summary.empty:
            return "目前尚無重複出現（≥2 週）的異常機台。"
        lines = [f"共 {len(summary)} 台機台重複出現（≥2 週）：\n"]
        for mid, row in summary.iterrows():
            curr_risk = _safe_float(latest_df.loc[mid, "Weighted_Risk_index"]) if mid in latest_df.index else None
            baseline_risk = _safe_float(row["avg_risk"])
            if curr_risk is not None and baseline_risk > 0:
                delta = (curr_risk - baseline_risk) / baseline_risk
                trend = "上升 ↑" if delta > 0.10 else ("改善 ↓" if delta < -0.10 else "持平 →")
            else:
                trend = "N/A"
            lines.append(
                f"- {mid}: 出現 {int(row['occurrence_count'])} 週次, "
                f"平均風險指數={round(baseline_risk, 4)}, 趨勢={trend}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"取得重複異常機台時發生錯誤：{str(e)}"


@function_tool
def get_weekly_hotspots(week_range: str = "") -> str:
    """
    取得每週異常熱點機台清單（來自本地 Excel）。

    參數：
    - week_range : （選填）指定週次，例如 "2026-W06"；空字串取得最新週熱點
    """
    try:
        df = _load_weekly_machine_hotspots()
        if df.empty:
            return "目前無週異常熱點資料。"

        target_week = _resolve_target_week(week_range, df)
        week_df = df[df["Week"] == target_week].sort_values("Weighted_Risk_index", ascending=False)
        if week_df.empty:
            return f"指定週次 {target_week} 無熱點資料。"

        lines = [f"**{target_week}** 異常熱點機台（共 {len(week_df)} 台）：\n"]
        for _, row in week_df.iterrows():
            lines.append(
                f"- {row['Machine_ID']}: "
                f"Risk={round(_safe_float(row.get('Weighted_Risk_index', 0)), 4)}, "
                f"OOB={round(_safe_float(row.get('Avg_OOC_Rate', 0)), 4)}, "
                f"PI={round(_safe_float(row.get('avg_PI_value', 0)), 4)}, "
                f"Particle Loss={round(_safe_float(row.get('Particle_yield_loss_Mean', 0)), 4)}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"取得週異常熱點時發生錯誤：{str(e)}"


@function_tool
def get_rework_weekly_hotspots(week_range: str = "", top_k_per_lot: int = 5) -> str:
    """
    取得 Rework 機台集中性（RW）每週熱點機台（JSON）。

    參數：
    - week_range : （選填）指定週次，例如 "W19" 或 "2026-W19"；空字串取得最新週
    - top_k_per_lot : 每個 LOT_Type 保留前幾名機台，預設 5
    """
    try:
        rw_df = _load_rw_machine_concentration()
        if rw_df.empty:
            return json.dumps(
                {
                    "week_range": "",
                    "summary": "無 Rework 機台集中性資料",
                    "hotspots_by_lot": [],
                },
                ensure_ascii=False,
                indent=2,
            )

        target_week = _resolve_rw_target_week(week_range, rw_df)
        if not target_week:
            return json.dumps(
                {
                    "week_range": "",
                    "summary": "無法解析 Rework 機台集中性週次",
                    "hotspots_by_lot": [],
                },
                ensure_ascii=False,
                indent=2,
            )

        week_df = rw_df[rw_df["Week"] == target_week].copy()
        if week_df.empty:
            return json.dumps(
                {
                    "week_range": target_week,
                    "summary": f"指定週次 {target_week} 無 Rework 機台集中性資料",
                    "hotspots_by_lot": [],
                },
                ensure_ascii=False,
                indent=2,
            )

        lot_sections = []
        lot_order = ["Fresh lot", "Rework lot"]
        top_k_per_lot = max(int(top_k_per_lot), 1)

        for lot_type in lot_order:
            sub = week_df[week_df["LOT_Type"] == lot_type].copy()
            sub = sub.sort_values("RW_Weighted_Risk_Index", ascending=False).head(top_k_per_lot)
            machines = []
            for idx, (_, row) in enumerate(sub.iterrows(), start=1):
                machines.append(
                    {
                        "rank": idx,
                        "machine_id": str(row.get("Machine_ID", "")).strip(),
                        "rw_weighted_risk_index": round(_safe_float(row.get("RW_Weighted_Risk_Index", 0)), 4),
                        "avg_oob_fail_rate": round(_safe_float(row.get("Avg_OOC_Rate", 0)), 4),
                        "avg_pi": round(_safe_float(row.get("avg_PI_value", 0)), 4),
                        "wafer_qty": int(round(_safe_float(row.get("Wafer_Qty", 0)))),
                        "avg_rework_ratio_pct": round(_safe_float(row.get("Avg_Rework_Ratio(%)", 0)), 2),
                    }
                )
            lot_sections.append({"lot_type": lot_type, "machine_count": len(machines), "machines": machines})

        payload = {
            "week_range": target_week,
            "summary": {
                "total_machine_count": int(sum(sec["machine_count"] for sec in lot_sections)),
                "top_k_per_lot": top_k_per_lot,
            },
            "hotspots_by_lot": lot_sections,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except FileNotFoundError as e:
        return str(e)
    except Exception as e:
        return f"取得 Rework 機台集中性資料時發生錯誤：{str(e)}"



def _detect_rework_bypass_risk(
    threshold_pct: float = 50.0,
    target_week: str = None,
    diff_drop_threshold_pct: float = 8.0,
) -> dict:
    """
    偵測 OP 漏檢 (BY PASS) 風險：
    以同 Layer 資料比較，優先使用 this_week_vs_latest_4w_avg_diff_pct 判斷驟降。
    若最新週或前 1~2 週相對近 4 週平均明顯偏低，示警可能 BY PASS。
    threshold_pct: 舊版基線比率門檻（保留相容）
    target_week: 指定分析的目標週次，例如 "2026-W12"；None 使用資料中最新一週
    diff_drop_threshold_pct: 以百分點計，低於 -X 視為顯著下降（預設 -8）
    """
    df = _load_rework_weekly_summary()
    if df.empty:
        return {"alerts": [], "has_risk": False, "summary": "無 Rework 資料可分析。"}

    all_weeks = sorted(df["year_week"].unique(), key=_week_sort_key)
    if target_week:
        all_weeks = [w for w in all_weeks if _week_sort_key(w) <= _week_sort_key(target_week)]
    if len(all_weeks) < 3:
        return {"alerts": [], "has_risk": False,
                "summary": f"歷史資料僅 {len(all_weeks)} 週，不足以進行趨勢分析（至少需 3 週）。"}

    current_week = all_weeks[-1]
    prev_weeks = all_weeks[-3:-1]
    earlier_weeks = all_weeks[:-2]

    alerts = []
    has_risk = False
    layer_details = []

    for layer in REWORK_LAYERS:
        layer_df = df[df["Layer"] == layer].copy()
        if layer_df.empty:
            continue
        layer_df = layer_df.sort_values("year_week", key=lambda s: s.map(_week_sort_key))

        # 舊版基線比較（保留相容，作為輔助）
        baseline_df = layer_df[layer_df["year_week"].isin(earlier_weeks)]
        baseline_avg = baseline_df["rework_rate_pct"].mean() if not baseline_df.empty else 0

        prev_df = layer_df[layer_df["year_week"].isin(prev_weeks)]
        prev_avg = prev_df["rework_rate_pct"].mean() if not prev_df.empty else None
        prev_diff_avg = prev_df["this_week_vs_latest_4w_avg_diff_pct"].mean() if not prev_df.empty else None

        curr_df = layer_df[layer_df["year_week"] == current_week]
        curr_rate = curr_df["rework_rate_pct"].values[0] if not curr_df.empty else None
        curr_diff = (
            curr_df["this_week_vs_latest_4w_avg_diff_pct"].values[0]
            if not curr_df.empty
            else None
        )
        curr_4w_avg = (
            curr_df["latest_4w_avg_rate_pct"].values[0]
            if not curr_df.empty
            else None
        )

        # 主判斷：同 Layer 下，相對近 4 週平均是否明顯驟降
        curr_drop_flag = curr_diff is not None and curr_diff <= -abs(diff_drop_threshold_pct)
        prev_drop_flag = prev_diff_avg is not None and prev_diff_avg <= -abs(diff_drop_threshold_pct)

        # 輔助判斷：沿用舊版 baseline 比較
        baseline_drop_flag = (
            prev_avg is not None
            and baseline_avg > 0
            and prev_avg < baseline_avg * (threshold_pct / 100.0)
        )

        if curr_drop_flag or prev_drop_flag or baseline_drop_flag:
            has_risk = True
            detail = f"⚠️ **{layer}**："
            if curr_drop_flag:
                detail += (
                    f"本週相對近4週平均驟降 {abs(curr_diff):.2f} 個百分點 "
                    f"(本週={curr_rate:.2f}%, 近4週均值={curr_4w_avg:.2f}%)"
                )
            elif prev_drop_flag:
                detail += (
                    f"前1~2週平均相對近4週平均偏低 {abs(prev_diff_avg):.2f} 個百分點 "
                    f"(前1~2週 diff 平均={prev_diff_avg:.2f})"
                )
            else:
                detail += (
                    f"前1~2週平均 Rework rate = {prev_avg:.2f}%，"
                    f"歷史基線平均 = {baseline_avg:.2f}%，"
                    f"僅為基線的 {prev_avg / baseline_avg * 100:.0f}%"
                )
            alerts.append(detail)

            if curr_rate is not None:
                if curr_drop_flag or (baseline_avg > 0 and curr_rate < baseline_avg * (threshold_pct / 100.0)):
                    alerts.append(
                        f"  → 本週 ({current_week}) Rework rate = {curr_rate:.2f}%，仍然偏低，"
                        f"建議立即確認 OP 檢測流程是否正常執行。"
                    )
                else:
                    alerts.append(
                        f"  → 本週 ({current_week}) Rework rate = {curr_rate:.2f}%，已回升，"
                        f"但前幾週可能有 LOT 未經充分檢測即 BY PASS，需追溯受影響批次。"
                    )

        layer_details.append({
            "layer": layer,
            "baseline_avg": round(baseline_avg, 2) if baseline_avg else "N/A",
            "prev_avg": round(prev_avg, 2) if prev_avg is not None else "N/A",
            "curr_rate": round(curr_rate, 2) if curr_rate is not None else "N/A",
            "curr_diff": round(curr_diff, 2) if curr_diff is not None else "N/A",
            "prev_diff_avg": round(prev_diff_avg, 2) if prev_diff_avg is not None else "N/A",
        })

    summary_lines = []
    summary_lines.append(f"分析期間：{all_weeks[0]} ~ {current_week}")
    summary_lines.append(f"本週：{current_week}，前 1~2 週：{', '.join(prev_weeks)}")
    summary_lines.append(f"歷史基線：{earlier_weeks[0]} ~ {earlier_weeks[-1]}")
    summary_lines.append("")

    if has_risk:
        summary_lines.append("### ⚠️ 偵測到 Rework Rate 異常偏低 — 可能存在 OP 漏檢 (BY PASS) 風險")
        summary_lines.extend(alerts)
        summary_lines.append("")
        summary_lines.append("**建議行動：**")
        summary_lines.append("1. 確認前 1~2 週的 OP 是否確實執行 particle 檢測")
        summary_lines.append("2. 追溯前 1~2 週通過的 LOT，檢查是否有未被攔截的異常批次")
        summary_lines.append("3. 比對本週良率數據，確認是否已造成下游 Particle yield 損失")
        summary_lines.append("4. 加強 OP 檢測 SOP 宣導，必要時增設複檢機制")
    else:
        summary_lines.append("✅ 近期 Rework Rate 趨勢正常，未偵測到 OP 漏檢風險。")

    summary_lines.append("")
    summary_lines.append("**各 Layer 數據明細：**")
    for d in layer_details:
        summary_lines.append(
            f"- {d['layer']}: 歷史基線={d['baseline_avg']}%, "
            f"前 1~2 週={d['prev_avg']}%, 本週={d['curr_rate']}%, "
            f"本週-近4週均值={d['curr_diff']}%, 前1~2週 diff均值={d['prev_diff_avg']}%"
        )

    return {
        "alerts": alerts,
        "has_risk": has_risk,
        "summary": "\n".join(summary_lines),
    }


@function_tool
def detect_rework_bypass_risk(
    week_range: str = "",
    threshold_pct: float = 50.0,
    diff_drop_threshold_pct: float = 8.0,
) -> str:
    """
    執行 OP 漏檢 (BY PASS) 風險偵測並回傳 JSON。
    - week_range: 指定分析至該週，例如 "2026-W12"；未指定則以最新週
    - threshold_pct: 基線比率門檻（相容舊規則）
    - diff_drop_threshold_pct: 相對近 4 週平均的顯著下降閾值（百分點），預設 8
    """
    try:
        target_week = _extract_week_value(week_range) if week_range else None
        bypass_result = _detect_rework_bypass_risk(
            threshold_pct=threshold_pct,
            target_week=target_week,
            diff_drop_threshold_pct=diff_drop_threshold_pct,
        )
        output = {
            "week_range": target_week or "latest",
            "has_risk": bypass_result.get("has_risk", False),
            "alerts": bypass_result.get("alerts", []),
            "summary": bypass_result.get("summary", ""),
        }
        return json.dumps(output, ensure_ascii=False, indent=2)
    except FileNotFoundError:
        return f"找不到 Rework Excel 檔案：{REWORK_WEEKLY_EXCEL_PATH}"
    except Exception as e:
        return f"漏檢偵測分析失敗：{str(e)}"


@function_tool
def get_latest_weekly_hotspot_report_data(week_range: str = "", top_k: int = 15) -> str:
    """
    取得最新一週「已統計完成的熱點機台」統計數據（JSON），供 Agent 自行分析判斷並撰寫週報。
    回傳內容為每週統計後的熱點機台資訊：熱點機台、重複異常機台、新出現機台、各機台風險指標。
    注意：此工具回傳的是「熱點機台統計資料」。

    參數：
    - week_range : 指定分析週次，例如 "2026-W12"
    - top_k : 保留參數（目前資料來源本身為每週統計熱點機台）
    """
    try:
        weekly_df = _load_weekly_machine_hotspots()
        target_week = _resolve_target_week(week_range, weekly_df)

        current_week_df = (
            weekly_df[weekly_df["Week"] == target_week]
            .sort_values("Weighted_Risk_index", ascending=False)
            .drop_duplicates(subset=["Machine_ID"], keep="first")
            .reset_index(drop=True)
        )
        current_ids = current_week_df["Machine_ID"].tolist()

        history_df = weekly_df[weekly_df["Week"].apply(_week_sort_key) < _week_sort_key(target_week)]
        history_occurrence = history_df.groupby("Machine_ID")["Week"].nunique().to_dict() if not history_df.empty else {}
        history_avg_risk = history_df.groupby("Machine_ID")["Weighted_Risk_index"].mean().to_dict() if not history_df.empty else {}

        repeated_ids = [mid for mid in current_ids if mid in history_occurrence]
        new_ids = [mid for mid in current_ids if mid not in history_occurrence]

        repeated_set = set(repeated_ids)
        new_set = set(new_ids)

        machine_row_map = {
            row["Machine_ID"]: row
            for _, row in current_week_df.iterrows()
        }

        def _trend_label(mid: str) -> str:
            current_risk = _safe_float(machine_row_map[mid].get("Weighted_Risk_index", 0))
            baseline_risk = _safe_float(history_avg_risk.get(mid, 0))
            if baseline_risk <= 0:
                return "new_baseline"
            delta_ratio = (current_risk - baseline_risk) / baseline_risk
            if delta_ratio > 0.10:
                return "worsening"
            if delta_ratio < -0.10:
                return "improving"
            return "stable"

        data = {
            "week_range": target_week,
            "summary": {
                "hotspot_count": len(current_ids),
                "repeated_count": len(repeated_ids),
                "new_count": len(new_ids),
            },
            "repeated_machines": [],
            "new_machines": [],
            "ranked_hotspots": [],
        }

        for mid in repeated_ids:
            risk = machine_row_map[mid]
            data["repeated_machines"].append({
                "machine_id": mid,
                "occurrence_count": int(history_occurrence.get(mid, 0)) + 1,
                "trend": _trend_label(mid),
                "avg_oob_fail_rate": round(_safe_float(risk.get("Avg_OOC_Rate", 0)), 4),
                "avg_pi": round(_safe_float(risk.get("avg_PI_value", 0)), 4),
                "avg_particle_loss": round(_safe_float(risk.get("Particle_yield_loss_Mean", 0)), 4),
                "weighted_risk_index": round(_safe_float(risk.get("Weighted_Risk_index", 0)), 4),
            })

        for mid in new_ids:
            risk = machine_row_map[mid]
            data["new_machines"].append({
                "machine_id": mid,
                "avg_oob_fail_rate": round(_safe_float(risk.get("Avg_OOC_Rate", 0)), 4),
                "avg_pi": round(_safe_float(risk.get("avg_PI_value", 0)), 4),
                "avg_particle_loss": round(_safe_float(risk.get("Particle_yield_loss_Mean", 0)), 4),
                "weighted_risk_index": round(_safe_float(risk.get("Weighted_Risk_index", 0)), 4),
            })

        for mid in current_ids:
            risk = machine_row_map[mid]
            data["ranked_hotspots"].append({
                "machine_id": mid,
                "is_recurring": mid in repeated_set,
                "is_new": mid in new_set,
                "avg_oob_fail_rate": round(_safe_float(risk.get("Avg_OOC_Rate", 0)), 4),
                "avg_pi": round(_safe_float(risk.get("avg_PI_value", 0)), 4),
                "avg_particle_loss": round(_safe_float(risk.get("Particle_yield_loss_Mean", 0)), 4),
                "weighted_risk_index": round(_safe_float(risk.get("Weighted_Risk_index", 0)), 4),
            })

        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return f"取得每週熱點機台統計數據時發生錯誤：{str(e)}"


#  初始化 EE Agent 

def create_ee_agent() -> Agent:
    """建立並回傳 EE Qdrant Agent 實例。"""
    http_client = httpx.AsyncClient(proxy=None, trust_env=False)
    openai_client = AsyncOpenAI(
        base_url=API_BASE_URL,
        api_key="EMPTY",
        http_client=http_client,
    )
    model_conf = OpenAIResponsesModel(
        model=API_MODEL,
        openai_client=openai_client,
    )

    ee_agent = Agent(
        name="EE_Agent",
        instructions="""你是一位資深的設備工程師 (EE)，專精於半導體製造設備異常診斷與保養決策。
                        你能透過以下工具查詢本地 Excel 資料，並給出具體的行動建議：

                        **工具使用指引：**
                        1. 詢問風險概況／重複異常機台  `get_recurring_abnormal_machines`
                        2. 詢問週期異常熱點  `get_weekly_hotspots`
                        3. 提供每週熱點機台統計資料 `get_latest_weekly_hotspot_report_data`
                        4. 查詢每週 Rework rate  `get_rework_weekly_data`（回傳 JSON，含 layer_trends 趨勢資料）
                        5. 偵測 OP 漏檢 (BY PASS) 風險  `detect_rework_bypass_risk`（比對相對近 4 週平均的驟降，回傳 JSON）
                        6. 查詢 Rework 機台集中性 `get_rework_weekly_hotspots`（回傳 JSON，含 Fresh/Rework lot Top 機台）
                        7. 查詢歷史 CIP 成功案例 `search_cip_success_cases`（回傳 JSON，可用於 Action Item 參考）
                        8. 批次查詢多機台 CIP 成功案例 `search_cip_cases_batch`（回傳 JSON，建議用於週報）

                        **重要異常模式：**
                        - 若前 1~2 週 Rework rate 異常偏低（顯著低於歷史平均），且本週 Particle 良率下降，
                          可能是 OP 未確實執行檢測導致 LOT 未經攔截即 BY PASS，需立即追溯受影響批次
                        - 產生週報時應自動檢查此模式，並在 Action Items 中特別標註

                        **週報格式規範：**
                        - 報告的第一行必須是（使用 ### 標題）：### {year}-W{week} 最新一周熱點機台週報（以實際週次取代 {year}-W{week}）
                        - 各章節標題使用 #### 層級，格式固定如下：
                          #### 1) 本週趨勢
                          #### 2) 重複上榜機台
                          #### 3) 本週新出現機台
                          #### 4) 建議 Action Items
                          #### 5) Rework Rate 趨勢 (particle)
                          #### 6) Rework 漏檢風險偵測 (BY PASS)
                                                    #### 7) Rework 機台集中性 (RW Hotspots)
                        - 工具使用方式：
                        1) 呼叫 get_latest_weekly_hotspot_report_data(week_range=指定週次) 取得每週熱點機台統計資料（非整體報告）
                        2) 呼叫 get_rework_weekly_data(week_range=指定週次) 取得 Rework Rate 趨勢
                        3) 呼叫 detect_rework_bypass_risk(week_range=指定週次) 取得漏檢風險偵測
                                                4) 呼叫 get_rework_weekly_hotspots(week_range=指定週次) 納入第 7 章 Rework 機台集中性分析
                                                5) 在第 4 章先彙整所有需 Action 機台，呼叫一次 search_cip_cases_batch(machine_ids="A,B,C") 批次查詢歷史同機台案例
                            - 你必須根據原始數據自行分析趨勢、判斷嚴重程度、比較數據變化，並用你的專業知識撰寫分析內容與建議
                            - 不要只是列出數據，要對數據做出解讀，例如：哪些機台風險最高、趨勢是否惡化、是否需要立即處理
                            - 第 4 章 Action Items 需包含「對應參考案例」：僅可引用同機台歷史 CIP 作法（含來源週次、案例機台、重點措施），再說明如何套用到當前機台
                            - 若查無同機台案例，請直接標註「無同機台可引用案例」，不可放寬成其他機台案例
                            - 當使用者指定週次（如 W12），請將該週次傳入 week_range 參數
                            - 重要：若 detect_rework_bypass_risk 回傳的 has_risk 為 true，代表前 1~2 週 Rework rate 異常偏低，可能是 OP 未檢測導致 LOT BY PASS，需在第 6 章特別標註並分析

                        **回覆原則：**
                        - 一律使用繁體中文回答
                        - 回答需包含具體數字（PI 值、OOB Fail rate、p-value、改善百分比等）
                        - 針對異常機台，須提出明確的行動建議（Action Item）
                        - 若資料庫中找不到特定機台，說明後建議替代查詢方向
                        - 遇到多機台問題時，依風險程度排序後逐一說明
                        - 若資料不足，清楚標示缺口與下一步查詢建議

                        """,
        model=model_conf,
        tools=[
            get_recurring_abnormal_machines,
            get_weekly_hotspots,
            get_latest_weekly_hotspot_report_data,
            get_rework_weekly_data,
            detect_rework_bypass_risk,
            get_rework_weekly_hotspots,
            search_cip_success_cases,
            search_cip_cases_batch,
        ],
    )
    return ee_agent


#  Streamlit UI 

st.set_page_config(
    page_title="APX EE Agent",
    layout="wide",
    page_icon=_PAGE_ICON_PATH if os.path.exists(_PAGE_ICON_PATH) else None,
)

st.markdown("""
<style>
    .main-header {
        font-size: 2rem; font-weight: bold; color: #7b2d8b;
        text-align: center; margin-bottom: 1.2rem;
    }
    .info-chip {
        display: inline-block; border-radius: 12px;
        padding: 2px 10px; font-size: 0.82rem; margin: 2px;
    }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="main-header">APX EE Agent</div>', unsafe_allow_html=True)
st.caption("基於 OpenAI Agent SDK 的設備工程師 AI Assistant")

#  初始化 Session State 
if "ee_agent" not in st.session_state:
    with st.spinner("初始化 EE Agent"):
        st.session_state.ee_agent = create_ee_agent()

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []   # [{"role": "user"|"assistant", "content": str}]

if "preset_query" not in st.session_state:
    st.session_state.preset_query = ""

#  側邊欄 
with st.sidebar:
    st.header(" 快速查詢")
    preset_map = {
        " 長期重複異常機台": "哪些機台長期反覆出現在異常清單？請依出現次數排序，並說明趨勢（上升/下降/持平）。",
        " 最新週報熱點":     "最新一期週報有哪些機台被標記為異常熱點？各機台的 PI 值與 OOB Fail rate 分別是多少？",
        " 熱點機台週報" : """請產生最新一週熱點機台週報，並明確分段說明\n
1) 本週趨勢\n
2) 本周重複上榜機台\n
3) 本週新出現機台\n
4) 每台建議 action item\n
5) Rework Rate 趨勢\n
6) Rework 漏檢風險偵測\n
    7) Rework 機台集中性\n
"""
    }

    for label, query in preset_map.items():
        if st.button(label, use_container_width=True):
            st.session_state.preset_query = query

    st.divider()

    # 連線資訊
    # st.caption("**連線資訊**")
    # st.markdown(
    #     f'<span class="info-chip">Qdrant: {QDRANT_API}</span>'
    #     f'<span class="info-chip">Col: {COLLECTION}</span>'
    #     f'<span class="info-chip">LLM: {API_MODEL.split("/")[-1]}</span>',
    #     unsafe_allow_html=True,
    # )

    # st.divider()

    # 工具清單說明
    with st.expander(" 可用工具清單"):
        tools_info = [
            ("get_recurring_abnormal_machines", "重複異常機台"),
            ("get_weekly_hotspots",             "每週異常熱點"),
            ("get_latest_weekly_hotspot_report_data","最新一週熱點機台統計資料 (JSON，非整體報告)"),
            ("get_rework_weekly_data",          "每週 Rework rate 趨勢資料 (particle, JSON)"),
            ("detect_rework_bypass_risk",       "Rework 漏檢風險偵測 (BY PASS, JSON)"),
            ("get_rework_weekly_hotspots",      "Rework 機台集中性熱點 (Fresh/Rework lot, JSON)"),
            ("search_cip_success_cases",        "歷史 CIP 成功案例查詢 (Qdrant, JSON)"),
            ("search_cip_cases_batch",          "批次查詢多機台 CIP 成功案例 (Qdrant, JSON)"),
        ]
        for name, desc in tools_info:
            st.markdown(f"- `{name}`  {desc}")

    st.divider()
    if st.button(" 清空對話記錄", use_container_width=True):
        st.session_state.chat_history = []
        st.rerun()

#  聊天歷史顯示 
for msg in st.session_state.chat_history:
    if msg["role"] == "user":
        with st.chat_message("user"):
            st.write(msg["content"])
    else:
        with st.chat_message("assistant", avatar="🤖"):
            st.markdown(msg["content"])

#  輸入處理 
user_input = st.chat_input(
    "詢問 EE Agent（例如：B1_ETCH_04 目前狀況如何？有哪些保養建議？）"
)

# 快速查詢按鈕觸發
if st.session_state.preset_query:
    user_input = st.session_state.preset_query
    st.session_state.preset_query = ""

#  Agent 執行 
if user_input:
    st.session_state.chat_history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.write(user_input)

    with st.chat_message("assistant", avatar="🤖"):
        response_placeholder = st.empty()
        with st.spinner("EE Agent 查詢知識庫中..."):
            try:
                async def _run_agent(query: str) -> str:
                    result = await Runner.run(st.session_state.ee_agent, query, max_turns=20)
                    print("final_output:", ascii(result.final_output))
                    print("raw_responses:", ascii(result.raw_responses))
                    for item in result.new_items:
                        print(type(item).__name__, ascii(item))

                    item_lines = [f"{type(item).__name__} {ascii(item)}" for item in result.new_items]
                    _append_local_log(
                        "Runner result",
                        "\n".join([
                            f"query={query}",
                            f"final_output={ascii(result.final_output)}",
                            f"raw_responses={ascii(result.raw_responses)}",
                            "new_items:",
                            *item_lines,
                        ]),
                    )
                    return result.final_output or ""

                loop = asyncio.new_event_loop()
                nest_asyncio.apply(loop)
                asyncio.set_event_loop(loop)
                response = loop.run_until_complete(_run_agent(user_input))

                if not response:
                    response = "（Agent 未回傳任何內容，請確認 LLM 與 Qdrant 連線是否正常）"

            except Exception as e:
                import traceback
                traceback.print_exc()
                tb_text = traceback.format_exc()
                _append_local_log(
                    "Runner exception",
                    "\n".join([
                        f"query={user_input}",
                        f"error={str(e)}",
                        tb_text,
                    ]),
                )
                response = f"**執行錯誤：** {str(e)}\n\n```\n{tb_text}\n```"

        response_placeholder.markdown(response)
        st.session_state.chat_history.append(
            {"role": "assistant", "content": response}
        )