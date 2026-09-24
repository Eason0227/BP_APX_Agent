"""
EE_Agent_api.py
------------------------------
提供 EE 週報與自然語言查詢服務。
"""

import json
import os
import re
import time
import traceback
import contextvars
from datetime import datetime
from typing import Any, Dict, List, Optional
import pandas as pd
import httpx
import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from agents import (
    Agent,
    Runner,
    function_tool,
    OpenAIResponsesModel,
    AsyncOpenAI,
)

session = requests.Session()
session.trust_env = False  # 忽略環境變數（包含 proxy）

QDRANT_API = "http://10.10.19.35:6333"
COLLECTION = "BP_APX"
DENSE_VEC = "Dense-vector"
SPARSE_VEC = "Sparse-vector"
EMBEDDING_API = "http://10.11.32.244:18089"

# ── 全域設定 ──
API_BASE_URL = "http://10.11.32.155:4000/v1"
API_KEY = "sk-vVPq-hpFctglwngOCf7bOA"
API_MODEL = "Qwen3.6-35B-A3B"
# API_MODEL = "Gemma4-31B"

# API_BASE_URL = "http://10.11.33.5:9120/v1"
# API_MODEL = "/model/gpt-oss-120b"
# API_KEY = "EMPTY"

# API_BASE_URL = "http://10.11.33.5:9988/v1"
# API_MODEL = "/model/gpt-oss-20b"

REWORK_WEEKLY_EXCEL_PATH = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_rework_output\weekly_rework_report_(Security C).xlsx"
REWORK_WEEKLY_SHEET_NAME = "weekly_summary" 
REWORK_LAYERS = ["PR1", "PR2", "uPad"]
WEEKLY_MACHINE_EXCEL_PATH = r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_report_top5_machine_(Security C).xlsx"
RW_MACHINE_CONCENTRATION_PATH = (
    r"D:\Paticle_OOB_system\Report_APP_BP\APX_Agent_Data\EE_Agent\weekly_rework_top5\Merged - 機台統計詳細資料_(Security C).xlsx"
)
CIP_COLLECTION = "BP_APX_Particle_Meeting_Reports"
USE_SYSTEM_PROXY = False
REQUESTS_PROXIES = None if USE_SYSTEM_PROXY else {"http": None, "https": None}

_TRACE_CONTEXT: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("trace_context", default=None)


def _to_jsonable(value: Any, max_depth: int = 4) -> Any:
    """將任意物件盡量轉為可 JSON 序列化型別，避免 trace 序列化失敗。"""
    if max_depth <= 0:
        return str(value)

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, max_depth=max_depth - 1) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v, max_depth=max_depth - 1) for v in value]

    if hasattr(value, "model_dump"):
        try:
            return _to_jsonable(value.model_dump(), max_depth=max_depth - 1)
        except Exception:
            pass

    if hasattr(value, "dict"):
        try:
            return _to_jsonable(value.dict(), max_depth=max_depth - 1)
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        try:
            return _to_jsonable(vars(value), max_depth=max_depth - 1)
        except Exception:
            pass

    return str(value)


def _append_trace(event_type: str, **payload: Any) -> None:
    trace_ctx = _TRACE_CONTEXT.get()
    if trace_ctx is None:
        return
    trace_ctx.setdefault("events", []).append(
        {
            "ts": datetime.now().isoformat(),
            "event": event_type,
            "data": _to_jsonable(payload),
        }
    )


def _snapshot_run_result(result: Any) -> dict:
    snapshot = {
        "type": type(result).__name__,
        "has_final_output": hasattr(result, "final_output"),
        "has_input": hasattr(result, "input"),
    }
    for attr in ["final_output", "input", "new_items", "last_agent"]:
        if hasattr(result, attr):
            try:
                snapshot[attr] = _to_jsonable(getattr(result, attr))
            except Exception:
                snapshot[attr] = f"<unavailable:{attr}>"
    return snapshot


def _get_dense_embedding(text: str) -> list:
    resp = requests.post(
        f"{EMBEDDING_API}/embed",
        json={"inputs": [text]},
        headers={"Content-Type": "application/json"},
        timeout=60,
        proxies={"http": None, "https": None},
    )
    resp.raise_for_status()
    return resp.json()[0]


def _qdrant_search(query_text: str, doc_type: str = None, machine_id: str = None, top_k: int = 5) -> list:
    embedding = _get_dense_embedding(query_text)

    must_conditions = []
    if doc_type:
        must_conditions.append({"key": "doc_type", "match": {"value": doc_type}})
    if machine_id:
        must_conditions.append({"key": "machine_id", "match": {"value": machine_id}})

    query_body = {
        "vector": {"name": DENSE_VEC, "vector": embedding},
        "limit": top_k,
        "with_payload": True,
    }
    if must_conditions:
        query_body["filter"] = {"must": must_conditions}

    resp = requests.post(
        f"{QDRANT_API}/collections/{COLLECTION}/points/search",
        json=query_body,
        headers={"Content-Type": "application/json"},
        timeout=60,
        proxies={"http": None, "https": None},
    )
    resp.raise_for_status()
    results = resp.json().get("result", [])
    return [
        {
            "score": r["score"],
            "content": r["payload"].get("page_content", ""),
            "payload": r["payload"],
        }
        for r in results
    ]


def _qdrant_scroll(doc_type: str, machine_id: str = None, limit: int = 20) -> list:
    must_conditions = [{"key": "doc_type", "match": {"value": doc_type}}]
    if machine_id:
        must_conditions.append({"key": "machine_id", "match": {"value": machine_id}})

    resp = requests.post(
        f"{QDRANT_API}/collections/{COLLECTION}/points/scroll",
        json={
            "filter": {"must": must_conditions},
            "limit": limit,
            "with_payload": True,
        },
        headers={"Content-Type": "application/json"},
        timeout=60,
        proxies={"http": None, "https": None},
    )
    resp.raise_for_status()
    points = resp.json().get("result", {}).get("points", [])
    return [p["payload"] for p in points]


def _format_search_results(results: list, max_items: int = 8) -> str:
    if not results:
        return "（未找到相關資料）"
    lines = []
    for r in results[:max_items]:
        lines.append(f"[相關度: {r['score']:.3f}]\n{r['content']}")
    return "\n\n---\n\n".join(lines)


def _extract_machine_id(payload: dict) -> str:
    for key in ("machine_id", "eqp_id", "tool_id", "machine"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    content = payload.get("page_content", "")
    if isinstance(content, str):
        match = re.search(r"[A-Z]\d_[A-Z]+_\d{2}", content)
        if match:
            return match.group(0)
    return ""


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


def _format_machine_metrics(risk_payload: dict) -> str:
    if not risk_payload:
        return ""
    oob = risk_payload.get("avg_oob_fail_rate", risk_payload.get("oob_fail_rate", risk_payload.get("avg_ooc_rate", "NA")))
    pi = risk_payload.get("avg_pi", risk_payload.get("pi_value", risk_payload.get("pi", "NA")))
    particle = risk_payload.get("avg_particle_loss", risk_payload.get("particle_loss", "NA"))
    return f"OOB Fail rate: {oob}, PI: {pi}, Particle Loss: {particle}"


def _build_action_item(machine_id: str, risk_payload: dict, is_recurring: bool, is_new: bool) -> str:
    metrics_text = _format_machine_metrics(risk_payload)
    action_parts = []
    if is_recurring:
        action_parts.append("連續異常，請優先安排 24 小時內深度點檢與參數回溯")
    if is_new:
        action_parts.append("新進異常，建議先執行基線比對與近期 recipe/lot 變更盤點")
    if not action_parts:
        action_parts.append("列入本週追蹤名單，維持日監控並確認改善趨勢")
    return f"- {machine_id}: {'；'.join(action_parts)}。{metrics_text}".strip()


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
    text = re.sub(r"[^A-Z0-9_]+", "_", str(value or "").upper())
    return re.sub(r"_+", "_", text).strip("_")


def _extract_machine_family(value: str) -> str:
    token = _normalize_machine_token(value)
    if not token:
        return ""
    parts = [p for p in token.split("_") if p]
    if len(parts) >= 3:
        return parts[1]
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-2]
    return parts[0] if parts else ""


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

    # 支援輸入 Week_Range 字串（例如: 2026-W06 ~ 2026-W07）
    if week_range and "Week_Range" in weekly_df.columns:
        matched_df = weekly_df[weekly_df["Week_Range"].astype(str).str.strip() == week_range.strip()]
        if not matched_df.empty:
            matched_weeks = sorted(matched_df["Week"].unique(), key=_week_sort_key)
            return matched_weeks[-1]

    return available_weeks[-1]


def _load_rw_machine_concentration() -> pd.DataFrame:
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


def _embed_query_dense(text: str) -> List[float]:
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
) -> List[dict]:
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
        url = f"{QDRANT_API}/collections/{CIP_COLLECTION}/points/query"
    else:
        body = {
            "vector": {"name": DENSE_VEC, "vector": dense_vec},
            "limit": top_k,
            "with_payload": True,
        }
        if qdrant_filter:
            body["filter"] = qdrant_filter
        url = f"{QDRANT_API}/collections/{CIP_COLLECTION}/points/search"

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


def _search_cip_core(
    machine_id: str,
    week_range: str = "",
    query_text: str = "particle contamination 改善 CIP 成功 案例",
    top_k: int = 5,
    agent_type: str = "",
    use_sparse: bool = False,
) -> dict:
    normalized_week = _extract_week_value(week_range)
    if not normalized_week and week_range:
        week_range_upper = str(week_range).strip().upper()
        normalized_week = week_range_upper if week_range_upper.startswith("W") else ""

    query_machine_token = _normalize_machine_token(machine_id)
    query_machine_family = _extract_machine_family(machine_id)

    hits = _search_knowledge_hits(
        query_text=query_text,
        top_k=max(int(top_k), 1) * 4,
        agent_type=(agent_type or None),
        equipment_id=(machine_id or None),
        meeting_week=None,
        use_sparse=bool(use_sparse),
    )

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

        exact_machine_match = bool(normalized_machine and normalized_machine in {ee_mid, pe_mid})
        family_machine_match = bool(query_machine_family and query_machine_family in {ee_family, pe_family})
        token_partial_match = bool(
            query_machine_token
            and (
                query_machine_token in ee_token
                or query_machine_token in pe_token
                or ee_token in query_machine_token
                or pe_token in query_machine_token
            )
        )

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

    return {
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


@function_tool
def search_cip_success_cases(
    machine_id: str = "",
    week_range: str = "",
    query_text: str = "particle contamination 改善 CIP 成功 案例",
    top_k: int = 5,
    agent_type: str = "",
    use_sparse: bool = False,
) -> str:
    tool_started = time.perf_counter()
    _append_trace(
        "tool_start",
        tool="search_cip_success_cases",
        machine_id=machine_id,
        week_range=week_range,
        top_k=top_k,
        agent_type=agent_type,
        use_sparse=use_sparse,
    )
    try:
        tool_output = json.dumps(
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
        _append_trace(
            "tool_done",
            tool="search_cip_success_cases",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            output_preview=tool_output[:2000],
            output_length=len(tool_output),
        )
        return tool_output
    except Exception as e:
        _append_trace(
            "tool_error",
            tool="search_cip_success_cases",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return f"查詢 CIP 成功案例時發生錯誤：{str(e)}"


@function_tool
def search_cip_cases_batch(
    machine_ids: str,
    week_range: str = "",
    query_text: str = "particle contamination 改善 CIP 成功 案例",
    top_k: int = 3,
    agent_type: str = "EE",
    use_sparse: bool = False,
) -> str:
    tool_started = time.perf_counter()
    _append_trace(
        "tool_start",
        tool="search_cip_cases_batch",
        machine_ids=machine_ids,
        week_range=week_range,
        top_k=top_k,
        agent_type=agent_type,
        use_sparse=use_sparse,
    )
    try:
        ids = [m.strip() for m in str(machine_ids).split(",") if m.strip()]
        if not ids:
            tool_output = json.dumps(
                {
                    "machine_ids": [],
                    "count": 0,
                    "results": {},
                    "message": "machine_ids 為空，請提供逗號分隔的機台代號。",
                },
                ensure_ascii=False,
                indent=2,
            )
            _append_trace(
                "tool_done",
                tool="search_cip_cases_batch",
                elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                output_preview=tool_output[:1000],
                output_length=len(tool_output),
            )
            return tool_output

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
        tool_output = json.dumps(
            {
                "machine_ids": ids,
                "count": len(ids),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        _append_trace(
            "tool_done",
            tool="search_cip_cases_batch",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            output_preview=tool_output[:2000],
            output_length=len(tool_output),
        )
        return tool_output
    except Exception as e:
        _append_trace(
            "tool_error",
            tool="search_cip_cases_batch",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return f"批次查詢 CIP 成功案例時發生錯誤：{str(e)}"


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

    # 欄位名標準化（去空白）
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = [
        "year_week",
        "Layer",
        "total_die",
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
        "total_die",
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

    # 相同週次/Layer 若有重複列，保留最後一筆
    df = df.drop_duplicates(subset=["year_week", "Layer"], keep="last")
    df = df.sort_values(["year_week", "Layer"], key=lambda s: s.map(_week_sort_key) if s.name == "year_week" else s)
    return df.reset_index(drop=True)


def _format_rework_summary(df: pd.DataFrame) -> str:
    """將 rework 彙整 DataFrame 格式化為可讀字串。"""
    if df.empty:
        return "（無 Rework 資料）"

    lines = []
    for week, wdf in df.groupby("year_week", sort=True):
        total_wafers = wdf["total_die"].sum()
        total_particle = wdf["particle_rework"].sum()
        overall_rate = round(total_particle / max(total_wafers, 1) * 100, 2)
        lines.append(f"**{week}**  整體 Rework rate (particle): {overall_rate}%  "
                     f"(particle={total_particle}, wafers={total_wafers})")
        for _, row in wdf.iterrows():
            lines.append(
                f"  - {row['Layer']}: rework rate={ round(row['rework_rate_pct']*100,2)}% "
                f"(particle={row['particle_rework']}, dies={row['total_die']})"
            )
    return "\n".join(lines)


def _build_rework_layer_trend_rows(df: pd.DataFrame, layers: List[str], trend_weeks: int = 6) -> List[dict]:
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
def get_latest_weekly_hotspot_report_data(week_range: str = "", top_k: int = 15) -> str:
    """
    取得最新一週「已統計完成的 Yield 熱點機台（Yield hotspots）」原始數據（JSON），供 Agent 自行分析判斷並撰寫週報。
    回傳內容為每週依 Yield 相關風險指標統計後的熱點機台資訊（非全部機台明細）：
    熱點機台、重複異常機台、新出現機台、各機台風險指標。

    參數：
    - week_range : 指定分析週次，例如 "2026-W12"
    - top_k : 保留參數（目前資料來源本身為每週統計熱點機台）
    """
    tool_started = time.perf_counter()
    _append_trace("tool_start", tool="get_latest_weekly_hotspot_report_data", week_range=week_range, top_k=top_k)
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

        # === 組裝原始數據 ===
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

        tool_output = json.dumps(data, ensure_ascii=False, indent=2)
        _append_trace(
            "tool_done",
            tool="get_latest_weekly_hotspot_report_data",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            output_preview=tool_output[:2000],
            output_length=len(tool_output),
        )
        return tool_output
    except Exception as e:
        _append_trace(
            "tool_error",
            tool="get_latest_weekly_hotspot_report_data",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return f"取得 Yield 熱點機台統計數據時發生錯誤：{str(e)}"


@function_tool
def get_rework_weekly_data(week_range: str = "", lookback_weeks: int = 4, trend_weeks: int = 6) -> str:
    """
    取得 Rework Rate 原始數據（JSON）。
    - week_range: 指定目標週次，例如 "2026-W12"；未指定則使用資料中最新週
    - lookback_weeks: 從目標週往前回看週數（含目標週）
    - trend_weeks: layer_trends 顯示最近趨勢週數

    """
    tool_started = time.perf_counter()
    _append_trace(
        "tool_start",
        tool="get_rework_weekly_data",
        week_range=week_range,
        lookback_weeks=lookback_weeks,
        trend_weeks=trend_weeks,
    )
    try:
        rw_df = _load_rework_weekly_summary()
        if rw_df.empty:
            output = {
                "week_range": _extract_week_value(week_range) or "",
                "weeks_analyzed": [],
                "detail": "無 Rework 資料",
                "layer_trends": [],
            }
            tool_output = json.dumps(output, ensure_ascii=False, indent=2)
            _append_trace(
                "tool_done",
                tool="get_rework_weekly_data",
                elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                output_preview=tool_output[:2000],
                output_length=len(tool_output),
            )
            return tool_output

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

        tool_output = json.dumps(output, ensure_ascii=False, indent=2)
        _append_trace(
            "tool_done",
            tool="get_rework_weekly_data",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            output_preview=tool_output[:2000],
            output_length=len(tool_output),
        )
        return tool_output
    except Exception as e:
        _append_trace(
            "tool_error",
            tool="get_rework_weekly_data",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return f"取得 Rework Rate 資料時發生錯誤：{str(e)}"


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
    - diff_drop_threshold_pct: 相對近 4 週平均的顯著下降閾值（百分點）
    """
    tool_started = time.perf_counter()
    _append_trace(
        "tool_start",
        tool="detect_rework_bypass_risk",
        week_range=week_range,
        threshold_pct=threshold_pct,
        diff_drop_threshold_pct=diff_drop_threshold_pct,
    )
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
        tool_output = json.dumps(output, ensure_ascii=False, indent=2)
        _append_trace(
            "tool_done",
            tool="detect_rework_bypass_risk",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            output_preview=tool_output[:2000],
            output_length=len(tool_output),
        )
        return tool_output
    except Exception as e:
        _append_trace(
            "tool_error",
            tool="detect_rework_bypass_risk",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return f"漏檢偵測分析失敗：{str(e)}"


@function_tool
def get_rework_weekly_hotspots(week_range: str = "", top_k_per_lot: int = 5) -> str:
    """
    取得 Rework 機台集中性（RW）每週熱點機台（JSON）。

    參數：
    - week_range : （選填）指定週次，例如 "W19" 或 "2026-W19"；空字串取得最新週
    - top_k_per_lot : 每個 LOT_Type 保留前幾名機台，預設 5
    """
    tool_started = time.perf_counter()
    _append_trace(
        "tool_start",
        tool="get_rework_weekly_hotspots",
        week_range=week_range,
        top_k_per_lot=top_k_per_lot,
    )
    try:
        rw_df = _load_rw_machine_concentration()
        if rw_df.empty:
            tool_output = json.dumps(
                {
                    "week_range": "",
                    "summary": "無 Rework 機台集中性資料",
                    "hotspots_by_lot": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            _append_trace(
                "tool_done",
                tool="get_rework_weekly_hotspots",
                elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                output_preview=tool_output[:1000],
                output_length=len(tool_output),
            )
            return tool_output

        target_week = _resolve_rw_target_week(week_range, rw_df)
        if not target_week:
            tool_output = json.dumps(
                {
                    "week_range": "",
                    "summary": "無法解析 Rework 機台集中性週次",
                    "hotspots_by_lot": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            _append_trace(
                "tool_done",
                tool="get_rework_weekly_hotspots",
                elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                output_preview=tool_output[:1000],
                output_length=len(tool_output),
            )
            return tool_output

        week_df = rw_df[rw_df["Week"] == target_week].copy()
        if week_df.empty:
            tool_output = json.dumps(
                {
                    "week_range": target_week,
                    "summary": f"指定週次 {target_week} 無 Rework 機台集中性資料",
                    "hotspots_by_lot": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            _append_trace(
                "tool_done",
                tool="get_rework_weekly_hotspots",
                elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
                output_preview=tool_output[:1000],
                output_length=len(tool_output),
            )
            return tool_output

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
        tool_output = json.dumps(payload, ensure_ascii=False, indent=2)
        _append_trace(
            "tool_done",
            tool="get_rework_weekly_hotspots",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            output_preview=tool_output[:2000],
            output_length=len(tool_output),
        )
        return tool_output
    except FileNotFoundError as e:
        _append_trace(
            "tool_error",
            tool="get_rework_weekly_hotspots",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
        )
        return str(e)
    except Exception as e:
        _append_trace(
            "tool_error",
            tool="get_rework_weekly_hotspots",
            elapsed_ms=round((time.perf_counter() - tool_started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        return f"取得 Rework 機台集中性資料時發生錯誤：{str(e)}"


def create_ee_agent() -> Agent:
    http_client = httpx.AsyncClient(proxy=None, trust_env=False)
    openai_client = AsyncOpenAI(
        base_url=API_BASE_URL,
        api_key= API_KEY,
        http_client=http_client,
    )

    model_conf = OpenAIResponsesModel(model=API_MODEL, openai_client=openai_client)

    return Agent(
        name="EE_Agent_Service",
        instructions="""你是一位資深的設備工程師 (EE)，專精於半導體製造設備異常診斷與保養決策。
你能透過工具查詢本地 Excel 與知識庫資料，並給出具體行動建議。

工具使用指引：
1. 呼叫 get_latest_weekly_hotspot_report_data 取得每週 Yield 熱點機台統計資料（Yield hotspots）
2. 呼叫 get_rework_weekly_data 取得每週 Rework rate 趨勢（JSON，含 layer_trends）
3. 呼叫 detect_rework_bypass_risk 偵測 OP 漏檢 (BY PASS) 風險
4. 呼叫 get_rework_weekly_hotspots 取得 Rework 機台集中性（Fresh/Rework lot）
5. 呼叫 search_cip_cases_batch 批次查詢多機台歷史 CIP 成功案例（用於第 4 章）

重要異常模式：
- 若前 1~2 週 Rework rate 異常偏低，且本週 Particle 良率下降，可能是 OP 未確實執行檢測導致 LOT BY PASS，需立即追溯受影響批次。

週報格式規範：
- 報告第一行必須使用 ### 
標題：### {year}-W{week} 整體機台狀態匯報（以實際週次取代 ）
- 各章節標題固定：
  #### 1) 本週趨勢
  #### 2) 重複上榜機台 (Yield hotspots)
  #### 3) 本週新出現機台 (Yield hotspots)
  #### 4) Rework 機台集中性 (RW Hotspots)
  #### 5) 建議 Action Items (RW Hotspots + Yield hotspots)
  #### 6) Rework Rate 趨勢 (particle)
  #### 7) Rework 漏檢風險偵測 (BY PASS)

週報工具流程：
1) get_latest_weekly_hotspot_report_data(week_range=指定週次)
2) get_rework_weekly_data(week_range=指定週次)
3) detect_rework_bypass_risk(week_range=指定週次)
4) get_rework_weekly_hotspots(week_range=指定週次)
5) 在第 5 章先彙整所有需 Action 機台，再呼叫 search_cip_cases_batch(machine_ids="A,B,C")

寫作要求：
- 一律使用繁體中文
- 必須自行分析趨勢與嚴重度，不可只貼數據
- 第 5 章需包含「對應參考案例」：僅可引用同機台歷史 CIP 作法並說明如何套用
- 若查無同機台案例，直接標註「無同機台可引用案例」，不可放寬為其他機台
- 使用者指定週次（如 W12）時，必須將該週次傳入 week_range
- 若 detect_rework_bypass_risk.has_risk 為 true，需在第 7 章特別標註與分析
- 若資料不足，需清楚標示缺口與下一步建議""",
        model=model_conf,
        tools=[
            get_latest_weekly_hotspot_report_data,
            get_rework_weekly_data,
            detect_rework_bypass_risk,
            get_rework_weekly_hotspots,
            search_cip_success_cases,
            search_cip_cases_batch,
        ],
    )


class AgentQueryRequest(BaseModel):
    query: str = Field(..., description="使用者查詢文字")
    include_trace: bool = Field(default=True, description="是否回傳執行 trace")


class WeeklyReportRequest(BaseModel):
    week_range: str = Field(default="", description="指定週期，例如 2026-W14")
    top_k: int = Field(default=15, ge=5, le=50)
    include_trace: bool = Field(default=True, description="是否回傳執行 trace")


class AgentResponse(BaseModel):
    output: str
    trace: Optional[dict] = None


app = FastAPI(title="EE Qdrant Agent API", version="1.0.0")
_AGENT_CACHE: Optional[Agent] = None


def _get_agent() -> Agent:
    global _AGENT_CACHE
    if _AGENT_CACHE is None:
        _AGENT_CACHE = create_ee_agent()
    return _AGENT_CACHE


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "EE_Agent_API"}

@app.post("/latest-week", response_model=AgentResponse)
async def latest_week_report(req: WeeklyReportRequest) -> AgentResponse:
    trace_ctx = {
        "request": {
            "endpoint": "/latest-week",
            "week_range": req.week_range,
            "top_k": req.top_k,
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = time.perf_counter()
    try:
        _append_trace("request_received", query_type="latest_week")
        agent = _get_agent()
        prompt = (
            "請產生最新一週機台週報，並明確分段說明第 1~7 章：\n"
            f"week_range={req.week_range or 'latest'}。"
        )
        _append_trace("runner_start", prompt=prompt)
        result = await Runner.run(agent, prompt)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        _append_trace("run_result_snapshot", snapshot=_snapshot_run_result(result))
        trace_ctx["summary"] = {
            "elapsed_ms": elapsed_ms,
            "output_length": len(result.final_output or ""),
        }
        return AgentResponse(output=result.final_output or "", trace=trace_ctx if req.include_trace else None)
    except Exception as e:
        _append_trace(
            "runner_error",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=f"latest_week_report failed: {str(e)}") from e
    finally:
        _TRACE_CONTEXT.reset(token)


@app.post("/chat", response_model=AgentResponse)
async def chat(req: AgentQueryRequest) -> AgentResponse:
    """以自然語言向 EE Agent 發問，取得設備工程觀點的分析回覆。"""
    trace_ctx = {
        "request": {
            "endpoint": "/chat",
            "query": req.query,
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    token = _TRACE_CONTEXT.set(trace_ctx)
    started = time.perf_counter()
    try:
        _append_trace("request_received", query_type="chat")
        agent = _get_agent()
        _append_trace("runner_start", prompt=req.query)
        result = await Runner.run(agent, req.query)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        _append_trace("runner_done", elapsed_ms=elapsed_ms)
        _append_trace("run_result_snapshot", snapshot=_snapshot_run_result(result))
        trace_ctx["summary"] = {
            "elapsed_ms": elapsed_ms,
            "output_length": len(result.final_output or ""),
        }
        return AgentResponse(output=result.final_output or "", trace=trace_ctx if req.include_trace else None)
    except Exception as e:
        _append_trace(
            "runner_error",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=f"chat failed: {str(e)}") from e
    finally:
        _TRACE_CONTEXT.reset(token)


# ========== 啟動 ==========
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8099)
