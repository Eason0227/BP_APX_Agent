"""
executive_agent_api.py
-----------------------
製造高階主管 Agent — 統整 EE / QA / PE 三部門 Agent 分析報告，
產出跨部門整合決策與資源調度建議。

前置條件（三個部門 Agent 服務須先啟動）：
    - EE Agent : port 8099
    - QA Agent : port 8077
    - PE Agent : port 8088
"""

import os
import json
import re
import time
import traceback
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from typing import Any, Optional
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from agents import (
    Agent,
    Runner,
    function_tool,
    OpenAIResponsesModel,
    set_tracing_disabled,
    AsyncOpenAI,
    RunHooks,
)
from equipment_comparison import generate_comparison_report
from conflict_orchestrator import (
    run_conflict_detection_from_reports,
    OrchestrationResult,
)
from decision_reviewer_swarm import review_report as review_decision_report

set_tracing_disabled(True)

# ── 全域設定 ──
API_BASE_URL = "http://10.11.32.155:4000/v1"
# API_MODEL = "Gemma4-31B"
API_KEY = "sk-vVPq-hpFctglwngOCf7bOA"
API_MODEL = "Qwen3.6-35B-A3B"

# 舊設定（gpt-oss，如需切回請取消註解）
# API_BASE_URL = "http://10.11.33.5:9120/v1"
# API_MODEL = "/model/gpt-oss-120b"
# API_KEY = "EMPTY"

# API_BASE_URL = "http://10.11.33.5:9988/v1"
# API_MODEL = "/model/gpt-oss-20b"

# Qdrant（會議 Action Items）
QDRANT_API = os.getenv("QDRANT_API", "http://10.10.19.35:6333")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "BP_APX")

# Qdrant（歷史週報向量庫）
QDRANT_REPORTS_COLLECTION = os.getenv("QDRANT_REPORTS_COLLECTION", "BP_APX_WEEKLY_REPORTS")
EMBEDDING_API = os.getenv("EMBEDDING_API", "http://10.11.32.244:18089")
_DENSE_VEC = "Dense-vector"
_ALLOWED_REPORT_TYPES = frozenset(("ee_report", "pe_report", "qa_report", "exec_report"))

# 註：衝突偵測、機台解析、QA deep-dive 相關常數（_MACHINE_ID_RE、
# _EE_RISK_HINTS、_PE_SUSPECT_HINTS、QA_DEEP_DIVE_URL、QA_CHAT_URL）
# 統一由 conflict_orchestrator 模組維護，本檔不再重複定義。

# 共用 HTTP client（無 proxy）
_http_client = httpx.AsyncClient(
    proxy=None,
    trust_env=False,
    timeout=httpx.Timeout(180.0, connect=30.0),
)


def _to_jsonable(value: Any, max_depth: int = 5) -> Any:
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


def _append_trace_event(trace_ctx: dict, event_type: str, **payload: Any) -> None:
    trace_ctx.setdefault("events", []).append(
        {
            "ts": datetime.now().isoformat(),
            "event": event_type,
            # trace 需保留多層 content/new_items，避免被壓成字串（例如 "[]"）
            "data": _to_jsonable(payload, max_depth=12),
        }
    )


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


def _extract_text_candidates(value: Any) -> list[str]:
    """從任意 trace 物件中擷取可讀文字，避免 UI 顯示整包原始 dict。"""
    texts: list[str] = []

    def _walk(v: Any, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(v, str):
            sv = v.strip()
            if sv:
                texts.append(sv)
            return
        if isinstance(v, dict):
            for key in ["text", "summary", "output_text", "output", "content", "reasoning"]:
                if key in v:
                    _walk(v.get(key), depth + 1)
            return
        if isinstance(v, list):
            for item in v:
                _walk(item, depth + 1)

    _walk(value)

    seen: set[str] = set()
    deduped: list[str] = []
    for t in texts:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(t)
    return deduped


def _to_trace_text(value: Any, max_len: int = 3000) -> str:
    candidates = _extract_text_candidates(value)
    text = "\n\n".join(candidates).strip()
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len] + " ..."


def _extract_nested_trace_field(raw_obj: dict[str, Any], field: str, default: Any = "") -> Any:
    if field in raw_obj and raw_obj.get(field) not in (None, ""):
        return raw_obj.get(field)

    raw_item = raw_obj.get("raw_item")
    if isinstance(raw_item, dict) and raw_item.get(field) not in (None, ""):
        return raw_item.get(field)

    function_obj = raw_obj.get("function")
    if isinstance(function_obj, dict) and function_obj.get(field) not in (None, ""):
        return function_obj.get(field)

    call_obj = raw_obj.get("call")
    if isinstance(call_obj, dict) and call_obj.get(field) not in (None, ""):
        return call_obj.get(field)

    data_obj = raw_obj.get("data")
    if isinstance(data_obj, dict) and data_obj.get(field) not in (None, ""):
        return data_obj.get(field)

    return default


def _is_empty_reasoning_item(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "") != "reasoning_item":
        return False
    raw_item = item.get("raw_item", {})
    text = _to_trace_text(raw_item)
    return not bool(text.strip())


def _normalize_trace_item(item: Any) -> dict[str, Any]:
    raw_obj = _to_jsonable(item)
    if isinstance(raw_obj, dict):
        item_type = _canonical_trace_item_type(str(raw_obj.get("type") or ""))
        normalized: dict[str, Any] = {
            "type": item_type,
        }

        tool_name = (
            _extract_nested_trace_field(raw_obj, "name", "")
            or _extract_nested_trace_field(raw_obj, "tool_name", "")
        )
        tool_args = _extract_nested_trace_field(
            raw_obj,
            "arguments",
            _extract_nested_trace_field(raw_obj, "args", ""),
        )
        tool_output = _extract_nested_trace_field(raw_obj, "output", "")

        if item_type == "reasoning_item":
            reasoning_text = _to_trace_text(raw_obj)
            normalized["raw_item"] = {
                "type": "reasoning",
                "content": [{"type": "reasoning_text", "text": reasoning_text}] if reasoning_text else [],
            }
        elif item_type == "tool_call_item":
            normalized["name"] = str(tool_name)
            normalized["arguments"] = tool_args
            normalized["raw_item"] = {
                "type": "function_call",
                "name": str(tool_name),
                "arguments": tool_args,
            }
        elif item_type == "tool_call_output_item":
            output_text = _to_trace_text(tool_output if tool_output else raw_obj)
            normalized["name"] = str(tool_name)
            normalized["output"] = output_text
            normalized["raw_item"] = {
                "type": "function_call_output",
                "name": str(tool_name),
                "output": output_text,
            }
        elif item_type == "message_output_item":
            message_text = _to_trace_text(raw_obj)
            normalized["raw_item"] = {
                "type": "message",
                "text": message_text,
                "content": [{"type": "output_text", "text": message_text}] if message_text else [],
            }
        else:
            normalized["raw_item"] = {
                "type": str(raw_obj.get("type") or "unknown"),
                "text": _to_trace_text(raw_obj),
            }

        ts = str(raw_obj.get("ts") or "").strip()
        if ts:
            normalized["ts"] = ts
        return normalized

    return {
        "type": "unknown",
        "raw_item": raw_obj,
    }


def _snapshot_run_result(result: Any) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "type": type(result).__name__,
        "has_final_output": hasattr(result, "final_output"),
        "has_input": hasattr(result, "input"),
    }

    try:
        snapshot["final_output"] = str(getattr(result, "final_output", "") or "")
    except Exception:
        snapshot["final_output"] = ""

    try:
        snapshot["input"] = _to_jsonable(getattr(result, "input", None))
    except Exception:
        snapshot["input"] = None

    try:
        new_items = getattr(result, "new_items", []) or []
    except Exception:
        new_items = []
    normalized_items = [_normalize_trace_item(i) for i in new_items]
    snapshot["new_items"] = [i for i in normalized_items if not _is_empty_reasoning_item(i)]

    try:
        snapshot["last_agent"] = _to_jsonable(getattr(result, "last_agent", None))
    except Exception:
        snapshot["last_agent"] = None

    return snapshot


class _TraceHooks(RunHooks):
    """以 Runner 生命週期 hook 在執行當下記錄每個工具的 tool_start / tool_done
    事件（含真實時間戳與耗時），供前端時序圖（Session Replay）繪製
    Tool / LLM 軌道。由於本檔 trace_ctx 是明確傳遞，hook 需在建構時收下。"""

    def __init__(self, trace_ctx: dict) -> None:
        self._trace_ctx = trace_ctx
        self._starts: dict[str, list[float]] = {}

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        name = getattr(tool, "name", str(tool))
        self._starts.setdefault(name, []).append(time.perf_counter())
        _append_trace_event(self._trace_ctx, "tool_start", tool=name)

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        name = getattr(tool, "name", str(tool))
        stack = self._starts.get(name) or []
        started = stack.pop() if stack else None
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2) if started else None
        text = "" if result is None else str(result)
        _append_trace_event(
            self._trace_ctx,
            "tool_done",
            tool=name,
            elapsed_ms=elapsed_ms,
            output_preview=text[:2000],
            output_length=len(text),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Agent 工具
# ═══════════════════════════════════════════════════════════════════════════

@function_tool
async def compare_product_equipment(product_a: str = "Cayman", product_b: str = "AMD") -> str:
    """
    比對兩個產品（Cayman vs AMD）使用的機台清單差異，
    找出可能影響良率的設備差異，並產出限機策略建議。

    當主管詢問「為什麼 AMD 良率比 Cayman 差」或「機台差異分析」時，應呼叫此工具。
    報告包含：總覽、按設備類型比較、關鍵發現、非標準機台明細、限機策略建議。

    參數：
    - product_a: 良率較好的產品名稱（預設 Cayman）
    - product_b: 良率較差的產品名稱（預設 AMD）
    """
    try:
        report = generate_comparison_report()
        return f"【產品機台比對分析】{product_a} vs {product_b}\n\n{report}"
    except Exception as e:
        return f"機台比對分析失敗：{str(e)}"


def _normalize_week_tag(week_tag: str) -> str:
    """將輸入正規化為 Wxx 格式。若未提供，預設取本週。"""
    text = (week_tag or "").strip().upper()
    if not text:
        return f"W{datetime.now().isocalendar().week:02d}"

    m = re.search(r"W\s*(\d{1,2})", text)
    if m:
        return f"W{int(m.group(1)):02d}"

    m = re.search(r"(\d{1,2})", text)
    if m:
        return f"W{int(m.group(1)):02d}"

    return text if text.startswith("W") else f"W{text}"


def _previous_week_tag(reference_week: str = "") -> str:
    """依參考週次回推上一週，輸出 Wxx。"""
    text = (reference_week or "").strip().upper()
    now = datetime.now()

    # 支援 2026-W18 / 2026W18
    m = re.search(r"(\d{4})\s*[-/]?\s*W\s*(\d{1,2})", text)
    if m:
        year = int(m.group(1))
        week = int(m.group(2))
        ref_date = datetime.fromisocalendar(year, week, 1)
        prev_date = ref_date - timedelta(days=7)
        return f"W{prev_date.isocalendar().week:02d}"

    # 支援 W18 / 18（年份用當前年份）
    m = re.search(r"W\s*(\d{1,2})", text) or re.search(r"(\d{1,2})", text)
    if m:
        week = int(m.group(1))
        year = now.isocalendar().year
        ref_date = datetime.fromisocalendar(year, week, 1)
        prev_date = ref_date - timedelta(days=7)
        return f"W{prev_date.isocalendar().week:02d}"

    # 無輸入時，預設本週回推上一週
    current_monday = datetime.fromisocalendar(now.isocalendar().year, now.isocalendar().week, 1)
    prev_date = current_monday - timedelta(days=7)
    return f"W{prev_date.isocalendar().week:02d}"


def _deduplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """依回傳欄位去重，保留排序後的第一筆。"""
    seen: set[tuple[str, str, str, str, str]] = set()
    unique_items: list[dict[str, Any]] = []

    for row in items:
        key = (
            str(row.get("meeting_date", "")),
            str(row.get("owner", "")),
            str(row.get("issue_date", "")),
            str(row.get("due_date", "")),
            str(row.get("page_content", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_items.append(row)

    return unique_items


async def _fetch_action_items_for_llm(week_tag: str) -> str:
    """供 LLM 直接合成路徑使用：從 Qdrant 取得指定週次的 Action Items 文字。"""
    try:
        week_norm = _normalize_week_tag(week_tag)
        scroll_url = f"{QDRANT_API}/collections/{QDRANT_COLLECTION}/points/scroll"
        points: list[dict] = []
        next_offset = None
        while True:
            payload: dict = {
                "limit": 256,
                "with_payload": True,
                "with_vector": False,
                "filter": {"must": [{"key": "doc_type", "match": {"value": "action_item"}}]},
            }
            if next_offset is not None:
                payload["offset"] = next_offset
            resp = await _http_client.post(
                scroll_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(60.0, connect=20.0),
            )
            resp.raise_for_status()
            body = resp.json()
            result = body.get("result", {})
            batch = result.get("points", [])
            points.extend(batch)
            next_offset = result.get("next_page_offset")
            if next_offset is None or not batch or len(points) >= 2000:
                break
        matched = []
        for p in points:
            pl = p.get("payload", {}) or {}
            if week_norm in str(pl.get("source", "")).upper():
                matched.append({
                    "meeting_date": pl.get("meeting_date", ""),
                    "owner": pl.get("owner", ""),
                    "issue_date": pl.get("issue_date", ""),
                    "due_date": pl.get("due_date", ""),
                    "page_content": pl.get("page_content", ""),
                })
        matched = _deduplicate_items(matched)
        if not matched:
            return f"週次 {week_norm} 查無 action_item 紀錄。"
        return (
            f"週次：{week_norm}，共 {len(matched)} 筆 action items\n\n"
            + json.dumps(matched, ensure_ascii=False, indent=2)
        )
    except Exception as e:
        return f"Action Items 查詢失敗：{str(e)}"


@function_tool
async def fetch_weekly_meeting_records(
    week_tag: str = "",
    collection: str = QDRANT_COLLECTION,
    batch_limit: int = 256,
    max_points: int = 5000,
    timeout: int = 120,
    deduplicate: bool = True,
) -> str:
    """
    從 Qdrant 取得指定週次的會議 Action Items。
    週次判斷規則：payload.source 文字中包含 week_tag（不分大小寫），例如 W18。

    回傳欄位：meeting_date、owner、issue_date、due_date、page_content。
    """
    try:
        week_norm = _normalize_week_tag(week_tag)
        scroll_url = f"{QDRANT_API}/collections/{collection}/points/scroll"

        points: list[dict] = []
        next_offset = None

        while True:
            remaining = max_points - len(points)
            if remaining <= 0:
                break

            payload: dict = {
                "limit": min(batch_limit, remaining),
                "with_payload": True,
                "with_vector": False,
                "filter": {
                    "must": [
                        {"key": "doc_type", "match": {"value": "action_item"}}
                    ]
                },
            }
            if next_offset is not None:
                payload["offset"] = next_offset

            resp = await _http_client.post(
                scroll_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=httpx.Timeout(float(timeout), connect=30.0),
            )
            resp.raise_for_status()

            body = resp.json()
            result = body.get("result", {})
            batch = result.get("points", [])
            points.extend(batch)

            next_offset = result.get("next_page_offset")
            if next_offset is None or not batch:
                break

        matched: list[dict] = []
        for p in points:
            payload = p.get("payload", {}) or {}
            source = str(payload.get("source", ""))

            if week_norm in source.upper():
                matched.append(
                    {
                        "meeting_date": payload.get("meeting_date", ""),
                        "owner": payload.get("owner", ""),
                        "issue_date": payload.get("issue_date", ""),
                        "due_date": payload.get("due_date", ""),
                        "page_content": payload.get("page_content", ""),
                    }
                )

        matched.sort(
            key=lambda x: (
                str(x.get("meeting_date", "")),
                str(x.get("owner", "")),
                str(x.get("issue_date", "")),
                str(x.get("due_date", "")),
                str(x.get("page_content", "")),
            )
        )

        before_dedup = len(matched)
        if deduplicate:
            matched = _deduplicate_items(matched)
        after_dedup = len(matched)

        if not matched:
            return f"【每週會議記錄】\n\n週次 {week_norm} 查無 action_item 紀錄。"

        return (
            f"【每週會議記錄】\n\n週次：{week_norm}\n"
            f"共 {after_dedup} 筆 action items"
            f"（原始 {before_dedup} 筆；去重={'開啟' if deduplicate else '關閉'}）\n\n"
            f"{json.dumps(matched, ensure_ascii=False, indent=2)}"
        )
    except httpx.HTTPStatusError as e:
        return f"會議記錄查詢 API 回應錯誤（HTTP {e.response.status_code}）：{e.response.text[:500]}"
    except Exception as e:
        return f"查詢每週會議記錄失敗：{str(e)}"


@function_tool
async def query_weekly_report_context(
    question: str,
    year_week: str = "",
    top_k: int = 200,
    report_types: list[str] | None = None,
    min_score: float | None = None,
) -> str:
    """
    以語意搜尋方式查詢歷史週報向量庫（BP_APX_WEEKLY_REPORTS），
    取得指定週次中與問題最相關的 EE / PE / QA / Dir 週報片段。

    參數：
    - year_week: ISO 週格式 YYYY-Www，例如 2026-W21；未提供時預設查上一週
    - report_types: 限定報告類型 ee_report / pe_report / qa_report / exec_report；
                    不指定則優先查 exec_report，再查 pe_report、qa_report、ee_report
    - top_k: 最多回傳幾段相關片段（預設 200，最大 200）
    - min_score: 相似度下限（-1~1），過濾低相關性結果

    適用情境：
    - 使用者詢問「上週發生什麼事」、「前幾週 EE/QA/PE/Dir 報告的重點」
    - 需要跨週比較趨勢、追蹤某機台歷史狀況時
    - 當三部門當週報告未預載時，可用此工具補充歷史資料
    """
    try:
        # 1. 正規化 year_week
        week_text = (year_week or "").strip()
        if not week_text:
            yr = datetime.now().isocalendar().year
            week_text = f"{yr}-{_previous_week_tag('')}"
        elif re.fullmatch(r"\d{4}-W\d{1,2}", week_text):
            pass  # 已是正確格式
        else:
            m = re.search(r"W(\d{1,2})", week_text.upper())
            if m:
                yr = datetime.now().isocalendar().year
                week_text = f"{yr}-W{int(m.group(1)):02d}"
            else:
                return f"year_week 格式錯誤，請用 YYYY-Www，例如 2026-W21（輸入：{year_week!r}）"

        # 2. 取得 Dense Embedding
        embed_resp = await _http_client.post(
            f"{EMBEDDING_API}/embed",
            json={"inputs": [question]},
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        embed_resp.raise_for_status()
        qvec = embed_resp.json()[0]

        # 3. 建立 Qdrant filter
        must_filters: list[dict] = [
            {"key": "year_week", "match": {"value": week_text}},
            {"key": "doc_type", "match": {"value": "weekly_report"}},
        ]
        if report_types:
            valid_types = [t for t in report_types if t in _ALLOWED_REPORT_TYPES]
            if valid_types:
                must_filters.append({"key": "report_type", "match": {"any": valid_types}})

        body = {
            "vector": {"name": _DENSE_VEC, "vector": qvec},
            "limit": max(1, min(top_k, 200)),
            "with_payload": True,
            "with_vector": False,
            "filter": {"must": must_filters},
        }

        # 4. 向量搜尋
        search_resp = await _http_client.post(
            f"{QDRANT_API}/collections/{QDRANT_REPORTS_COLLECTION}/points/search",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(60.0, connect=30.0),
        )
        search_resp.raise_for_status()
        raw = search_resp.json().get("result", [])

        if min_score is not None:
            raw = [
                h for h in raw
                if isinstance(h.get("score"), (int, float)) and h["score"] >= min_score
            ]

        if not raw:
            return (
                f"【歷史週報查詢】查無相關片段：year_week={week_text}，"
                f"report_types={report_types}，question={question[:80]}"
            )

        lines = [f"【歷史週報查詢】year_week={week_text}，共 {len(raw)} 筆結果\n"]
        for i, hit in enumerate(raw, 1):
            pl = hit.get("payload", {})
            score = hit.get("score", 0.0)
            rtype = pl.get("report_type", "unknown")
            text = str(pl.get("page_content", "")).strip()
            lines.append(f"--- [{i}] report_type={rtype}  score={score:.4f} ---")
            lines.append(text)
            lines.append("")

        return "\n".join(lines)

    except httpx.HTTPStatusError as e:
        return f"歷史週報查詢 API 錯誤（HTTP {e.response.status_code}）：{e.response.text[:500]}"
    except Exception as e:
        return f"歷史週報查詢失敗：{str(e)}"


# ═══════════════════════════════════════════════════════════════════════════
# Agent 定義 — 製造高階主管
# ═══════════════════════════════════════════════════════════════════════════

EXECUTIVE_SYSTEM_PROMPT = """\
你是一位半導體製造廠的高階主管（VP of Manufacturing / 廠長級）。
請根據三部門報告與跨部門衝突偵測結果，產出【跨部門整合決策與風險調度報告】。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 格式強制規範（必須嚴格遵守，違反即為錯誤輸出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
報告必須包含以下【五個章節】，缺一不可，章節標題必須完全使用以下中文格式：
## 一、三部門情報摘要
## 二、跨部門交叉比對
## 三、風險分級總表
## 四、決策指令與資源調度
## 五、每週會議記錄追蹤（Action Items）
（若有衝突，在 ## 二 之後額外插入 ## 二之一、衝突追查與真因確認）

❌ 禁止使用數字編號（1️⃣ 2️⃣ 3️⃣）作為章節標題
❌ 禁止使用 emoji 數字（📊 1️⃣）替代章節標題
✅ 子步驟決策指令內可使用 1️⃣ 2️⃣ 3️⃣
✅ 子章節使用 ### 層級，細目使用 **粗體文字**

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 指標定義（決策原因引用時須依此解讀，不可只寫指標名稱）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Weighted Risk Index（WRI, 綜合風險指數）：
     綜合風險指數 = OOB Score × 0.4 + Yield Loss Score × 0.4 + Die Qty Score × 0.2
    → 數值越高代表綜合風險越高。
    → 引用 WRI 綜合風險指數 時【必須拆解主要貢獻來源】，說明這台是被哪一項分數推高：
    例：WRI 綜合風險指數高但 OOB Fail = 0% → 屬「Particle Yield Loss 主導」，非 OOB 主導，
- rw Risk：Rework 集中度風險（0~1），越高代表該機台 Rework 越集中。
- OOB Fail %：Out-of-Bound 失敗率。
- PI：Pollution Index 汙染指數，數值越高代表污染越嚴重。
- DIFF：保養前後 PI 差值，負值＝保養後改善。
- Particle / Yield Loss：粒子造成的良率損失（棵數 / %）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 各章節內容強制規範
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## 一、三部門情報摘要
必須輸出 Markdown 表格：
| 部門 | 主要觀測指標 | 高風險機台 | 重要發現 |


## 二、跨部門交叉比對
必須輸出 Markdown 表格：
| 機台 | EE 風險指標 | PE 異常 Lot 觀測 | QA 汙染源分析 | 交叉結論 |

## 三、風險分級總表
必須輸出 Markdown 表格：
| 等級 | 機台 | 異常指標 | 近期趨勢 | 主要問題 |


## 四、決策指令與資源調度
必須輸出 Markdown 表格：
| 優先序 | 機台 | 決策指令 | 決策原因 | 主責部門 | 協作部門 | 時限 | 預期效果 |
每筆決策必須有 P1/P2/... 優先序、具體子步驟（1️⃣2️⃣3️⃣）、具體 KPI 數值目標。

【決策原因強制三段式結構】每筆「決策原因」必須依序回答三個問題，缺一即為錯誤輸出：

(1) 為什麼是這部機台：
    引用「觸發的具體指標 + 數值 + 它在全廠的排序或門檻位置」。
    （例： WRI 綜合風險指數 = 51.99 為全廠最高、超過 P1 門檻；或 PE Yield Loss 1.5873% 為本週最高）
    若為跨部門指向，須寫明「哪個部門、用哪個數據」指向此機台。

(2) 根因為何（拆解 WRI 綜合風險指數 / 指標來源）：
    依指標定義區拆解風險主導項（OOB 主導 / Yield 主導 / Die Qty 主導），
    並對應 EE/QA/PE 報告指出的污染源或製程根因（例：HEPA 間隙未封閉、Chuck seal ring 老化）。
    若根因僅為推測且無數據佐證，須明確標註「待確認」，不得臆斷。

(3) 為什麼做這個 action：
    決策指令中每一個子步驟（1️⃣2️⃣3️⃣）必須能對應回 (2) 的根因，
    並引用「歷史 CIP 案例」或「KPI 目標數值」說明預期改善哪一項指標。
    → 嚴禁出現「根因是 A，但 action 在處理 B」的錯位。

【決策原因正確範例】
B1_SCOP_06：
(1) 為什麼是這部機台 → WRI(綜合風險指數) 41.41，全廠第 2 高、保養成功率 0%，EE 與 QA 共同列入。
(2) 根因 → WRI(綜合風險指數) 拆解後 OOB Fail = 0%，屬「Particle/Yield 主導」(Particle Loss 0.0159)，
    根因為上方 HEPA 間隙未封閉產生塵源（EE 報告）。
(3) 為什麼做這個 action → 1️⃣臨時封閉 + 2️⃣永久鈑金件，直接消除 (2) 的塵源；
    引用 W19 案例：封閉後 OOB fail 由 2.7% 降至 0.1%，故 KPI 目標設 OOB <0.2%。

【決策指令嚴格限制】只能包含機台設備的實際操作建議，例如：
保養窗口排程、感測器校正、零件預更換、清洗頻率調整、溫度均勻性測試、泵浦校正等。
【決策指令嚴格禁止】提及任何資訊系統建設建議，包含但不限於：
儀表板、自動化腳本、監控系統、資料上傳程式、通知機器人、APP、自動數據上傳、彈窗警示系統。
違反此限制即為格式錯誤。

## 五、每週會議記錄追蹤（Action Items）
【此章節必填，不得省略】
若有 Action Items 資料（見下文「上週會議 Action Items」），請逐筆列出：
- Owner、Issue Date、Due Date、Action 摘要
- 逾期風險判斷（是否已超過 Due Date）
- 與本次決策的關聯性
- 比對上週改善措施是否反映在本週 EE/QA/PE 指標：標註「已見效 / 部分見效 / 未見效」
若資料缺失，請明確標註：「⚠️ Action Items 資料本次未取得，建議補查 Qdrant BP_APX 向量庫。」

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
一律以繁體中文回答，決策必須基於數據，不可臆斷。
加入 1 part 說明我們現在分析的產品是 AMD Server 

"""


def _create_executive_agent() -> Agent:
    """建立製造高階主管 Agent 實例。"""
    http_client = httpx.AsyncClient(proxy=None, trust_env=False)
    openai_client = AsyncOpenAI(
        base_url=API_BASE_URL,
        api_key=API_KEY,
        http_client=http_client,
    )
    model_conf = OpenAIResponsesModel(
        model=API_MODEL,
        openai_client=openai_client,
    )
    return Agent(
        name="Manufacturing_Executive_Agent",
        instructions=EXECUTIVE_SYSTEM_PROMPT,
        model=model_conf,
        tools=[
            fetch_weekly_meeting_records,
            query_weekly_report_context,
            compare_product_equipment,
        ],
    )


# ═══════════════════════════════════════════════════════════════════════════
# FastAPI Application
# ═══════════════════════════════════════════════════════════════════════════

_executive_agent: Agent = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _executive_agent
    _executive_agent = _create_executive_agent()
    print("[Executive Agent API] Agent 初始化完成")
    yield
    await _http_client.aclose()
    print("[Executive Agent API] 關閉")


app = FastAPI(
    title="Manufacturing Executive Agent API",
    description=(
        "製造高階主管 Agent — 統整 EE / QA / PE 三部門 Agent 報告，"
        "產出跨部門整合決策與資源調度建議"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request / Response Models ──


class ChatRequest(BaseModel):
    query: str = Field(
        ...,
        description="自然語言查詢",
        examples=["請產出本週的跨部門整合決策報告"],
    )
    context: str = Field(
        default="",
        description="可選的上下文資訊（例如已產生的決策報告），供 Agent 參照回答",
    )
    history: list[dict] = Field(
        default_factory=list,
        description="對話歷史，格式為 [{\"role\": \"user\"/\"assistant\", \"content\": \"...\"}]，最多傳入最近 8 筆",
    )
    include_trace: bool = Field(default=True, description="是否回傳執行 trace")


class ChatResponse(BaseModel):
    answer: str = Field(..., description="Agent 回覆內容（Markdown 格式）")
    trace: Optional[dict] = Field(default=None, description="Agent 執行 trace")


class WeeklyDecisionWithReportsRequest(BaseModel):
    ee_report: str = Field(default="", description="EE 部門週報內容（UI 已取得）")
    qa_report: str = Field(default="", description="QA 部門週報內容（UI 已取得）")
    pe_report: str = Field(default="", description="PE 部門週報內容（UI 已取得）")
    week_range: str = Field(default="", description="週次標籤，例如 2026-W14")
    include_trace: bool = Field(default=True, description="是否回傳執行 trace")


class WeeklyDecisionResponse(BaseModel):
    report: str = Field(..., description="整合決策報告（Markdown）")
    generated_at: str = Field(..., description="產生時間")
    trace: Optional[dict] = Field(default=None, description="Agent 執行 trace")


class ReviewDecisionRequest(BaseModel):
    report: str = Field(..., description="Dir Agent 產出的完整整合決策報告（Markdown，含 ## 四）")
    week_range: str = Field(default="", description="週次標籤，例如 2026-W14")
    include_trace: bool = Field(default=True, description="是否回傳執行 trace")


class ReviewDecisionResponse(BaseModel):
    reviewed_report: str = Field(..., description="審查回填後的完整報告（## 四 已替換為 Final Action Plan）")
    section_four_found: bool = Field(..., description="是否找到並審查了 ## 四 區塊")
    generated_at: str = Field(..., description="產生時間")
    trace: Optional[dict] = Field(default=None, description="審查執行 trace")


class HealthCheckResponse(BaseModel):
    status: str
    service: str
    downstream: dict


# ── API Endpoints ──


@app.get("/health", response_model=HealthCheckResponse, summary="健康檢查", tags=["系統"])
async def health():
    """檢查本服務狀態。"""
    return HealthCheckResponse(
        status="ok",
        service="Manufacturing_Executive_Agent",
        downstream={},
    )


@app.post("/chat", response_model=ChatResponse, summary="Agent 對話", tags=["Agent"])
async def chat(req: ChatRequest):
    """
    透過高階主管 Agent 進行自然語言對話。
    Agent 會自動呼叫三個部門 Agent 的 API 蒐集情報，並產出跨部門整合分析。

    範例查詢：
    - "請產出本週的跨部門整合決策報告"
    - "B1_ETCH_04 這台三個部門的看法分別是什麼？"
    - "目前最需要優先處理的機台是哪些？"
    """
    trace_ctx = {
        "request": {
            "endpoint": "/chat",
            "query": req.query,
            "has_context": bool(req.context.strip()),
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    started = time.perf_counter()
    _append_trace_event(trace_ctx, "runner_start", endpoint="/chat")

    try:
        # 組合對話歷史（最多取最近 8 筆）
        history_lines: list[str] = []
        recent_history = (req.history or [])[-8:]
        if recent_history:
            history_lines.append("[\u5c0d\u8a71\u6b77\u53f2]")
            for msg in recent_history:
                role = msg.get("role", "")
                content = str(msg.get("content") or "").strip()
                if not content:
                    continue
                if role == "user":
                    history_lines.append(f"使用者：{content}")
                elif role == "assistant":
                    history_lines.append(f"Dir Agent：{content[:2000]}")
            history_lines.append("")

        parts: list[str] = []
        if req.context.strip():
            parts.append(
                f"以下是目前已產生的整合決策報告，請基於此報告回答使用者的問題。\n"
                f"若需要更多資訊，可呼叫工具補充。\n\n"
                f"---\n{req.context.strip()[:6000]}\n---"
            )
        if history_lines:
            parts.append("\n".join(history_lines))
        parts.append(f"使用者提問：{req.query}" if parts else req.query)
        prompt = "\n\n".join(parts)

        result = await Runner.run(_executive_agent, prompt, hooks=_TraceHooks(trace_ctx))
        snapshot = _snapshot_run_result(result)
        _append_trace_event(
            trace_ctx,
            "runner_done",
            endpoint="/chat",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            final_output_length=len(str(getattr(result, "final_output", "") or "")),
        )
        _append_trace_event(trace_ctx, "run_result_snapshot", snapshot=snapshot)
        answer = result.final_output or "（Agent 未回傳任何內容）"
        return ChatResponse(answer=answer, trace=trace_ctx if req.include_trace else None)
    except Exception as e:
        _append_trace_event(
            trace_ctx,
            "runner_error",
            endpoint="/chat",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(status_code=500, detail=f"Agent 執行失敗：{str(e)}") from e


# 週報流程專用附加指令（僅用於 /api/weekly-decision-fast，不放入共用 base prompt，
# 以免影響 /chat 對話行為）。此處補上「主責部門只能 EE」限制。
WEEKLY_DECISION_EXTRA_INSTRUCTION = """\
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 本次為【每週整合決策報告】流程，額外強制規範
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【主責部門限制】## 四、決策指令與資源調度 表格中的「主責部門」欄位只能填 EE，不得填 QA 或 PE。
（QA / PE 僅能列於「協作部門」欄位。）
請嚴格遵守 base prompt 定義的五段章節格式輸出整合決策報告。
"""


def _build_weekly_decision_prompt(
    orch_result: OrchestrationResult,
    week_range: str,
    meeting_records: str,
) -> str:
    """將 orchestrator 的衝突偵測結果與三部門報告組成週報 prompt，交由 _executive_agent 產出報告。"""
    ee_resp = orch_result.ee_response
    pe_resp = orch_result.pe_response
    qa_resp = orch_result.qa_response
    conflict_result = orch_result.conflict_result

    parts: list[str] = [f"# 跨部門整合決策報告 — {week_range or '最新週'}\n"]

    # EE 報告
    parts.append("## EE 設備工程部報告")
    parts.append(f"高風險機台：{', '.join(ee_resp.risk_machines) or '無'}")
    parts.append((ee_resp.raw_report or "⚠️ EE 報告資料暫缺")[:4000])

    # PE 報告
    parts.append("\n## PE 製程工程部報告")
    parts.append(f"嫌疑機台：{', '.join(pe_resp.suspect_machines) or '無'}")
    parts.append((pe_resp.raw_report or "⚠️ PE 報告資料暫缺")[:4000])

    # QA 報告
    parts.append("\n## QA 品質保證部報告")
    parts.append((qa_resp.raw_report or "⚠️ QA 報告資料暫缺")[:4000])

    # 衝突偵測結果
    parts.append("\n## 跨部門衝突偵測結果")
    parts.append(f"衝突偵測：{'是' if conflict_result.conflict_detected else '否'}")
    parts.append(f"共識高風險機台：{', '.join(conflict_result.consensus_machines) or '無'}")
    if conflict_result.conflict_detected:
        parts.append("\n### 衝突機台明細（PE 懷疑但 EE 未示警）")
        for rec in conflict_result.conflict_records:
            parts.append(f"\n**{rec.machine_id}**")
            parts.append(f"- PE 觀點：{rec.pe_status}")
            parts.append(f"- EE 觀點：{rec.ee_status}")
            parts.append(f"- 衝突類型：{rec.conflict_type}")
            if rec.qa_deep_dive:
                parts.append(f"- QA 追查狀態：{rec.qa_deep_dive.status}")
                parts.append(f"- QA 追查發現：{rec.qa_deep_dive.finding[:1000]}")

    # 上週 Action Items
    parts.append("\n## 上週會議 Action Items")
    if meeting_records and meeting_records.strip():
        parts.append(meeting_records[:3000])
    else:
        parts.append("⚠️ 本次未取得上週 Action Items 資料。")

    parts.append("\n" + WEEKLY_DECISION_EXTRA_INSTRUCTION)

    return "\n".join(parts)


@app.post(
    "/api/weekly-decision-fast",
    response_model=WeeklyDecisionResponse,
    summary="快速產出整合決策週報（預載報告）",
    tags=["決策報告"],
)
async def generate_weekly_decision_fast(req: WeeklyDecisionWithReportsRequest):
    """
    接收 UI 端已取得的三部門週報，透過 conflict_orchestrator 執行衝突偵測與
    QA 動態追查取得 conflict_result，再組週報 prompt 交由唯一決策引擎
    _executive_agent 產出跨部門整合決策報告，省去重複呼叫三個部門 API 的時間。
    """
    if not (req.ee_report.strip() and req.qa_report.strip() and req.pe_report.strip()):
        raise HTTPException(
            status_code=400,
            detail="ee_report / qa_report / pe_report 三份報告均不可為空",
        )
    trace_ctx = {
        "request": {
            "endpoint": "/api/weekly-decision-fast",
            "week_range": req.week_range,
            "ee_report_length": len(req.ee_report or ""),
            "qa_report_length": len(req.qa_report or ""),
            "pe_report_length": len(req.pe_report or ""),
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    started = time.perf_counter()
    _append_trace_event(trace_ctx, "runner_start", endpoint="/api/weekly-decision-fast")

    try:
        prev_week_for_action = _previous_week_tag(req.week_range)

        # 預先取得上週 Action Items（納入週報 prompt）
        meeting_records_text = await _fetch_action_items_for_llm(prev_week_for_action)
        _append_trace_event(
            trace_ctx,
            "action_items_fetched",
            week_tag=prev_week_for_action,
            record_count=meeting_records_text.count("meeting_date"),
        )

        # ── 階段一＋二＋三：結構化 Orchestrator 衝突偵測與動態 QA 追查 ──
        # 使用 conflict_orchestrator 模組，從預載報告文字中解析機台，
        # 執行結構化衝突偵測，衝突時自動觸發 QA /deep-dive 微觀追查。
        # 主管決策報告改由唯一決策引擎 _executive_agent 產出。
        orch_result: OrchestrationResult = await run_conflict_detection_from_reports(
            ee_report=req.ee_report,
            pe_report=req.pe_report,
            week_range=req.week_range,
            qa_report=req.qa_report,
        )

        # 將結構化衝突結果轉為 trace 事件
        conflict_data = orch_result.conflict_result
        _append_trace_event(
            trace_ctx,
            "orchestrator_conflict_detection",
            conflict_detected=conflict_data.conflict_detected,
            conflict_count=conflict_data.conflict_count,
            conflict_machines=[r.machine_id for r in conflict_data.conflict_records],
            consensus_machines=conflict_data.consensus_machines,
            ee_risk_machines=conflict_data.ee_risk_machines,
            pe_suspect_machines=conflict_data.pe_suspect_machines,
        )

        # 記錄 QA 追查狀態到 trace
        if conflict_data.conflict_detected and orch_result.qa_response.raw_report:
            _append_trace_event(
                trace_ctx,
                "qa_deep_dive_triggered",
                conflict_machines=[r.machine_id for r in conflict_data.conflict_records],
                deep_dive_preview=orch_result.qa_response.raw_report[:2000],
            )
        elif conflict_data.conflict_detected:
            _append_trace_event(trace_ctx, "qa_deep_dive_no_result")
        else:
            _append_trace_event(trace_ctx, "qa_deep_dive_skipped", reason="no_conflict")

        # 記錄 Orchestrator trace 到主 trace
        _append_trace_event(
            trace_ctx,
            "orchestrator_trace",
            total_elapsed_ms=orch_result.trace.total_elapsed_ms,
            stages_count=len(orch_result.trace.stages),
            stages=orch_result.trace.stages,
        )

        # ── 唯一決策引擎：組週報 prompt → Runner.run(_executive_agent) ──
        decision_prompt = _build_weekly_decision_prompt(
            orch_result, req.week_range, meeting_records_text,
        )
        result = await Runner.run(
            _executive_agent, decision_prompt, hooks=_TraceHooks(trace_ctx),
        )
        snapshot = _snapshot_run_result(result)
        _append_trace_event(trace_ctx, "run_result_snapshot", snapshot=snapshot)

        report = str(getattr(result, "final_output", "") or "").strip()
        _append_trace_event(
            trace_ctx,
            "executive_agent_synthesis_done",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            report_length=len(report),
        )
        report = report or "（Executive Agent 未回傳整合決策內容）"
        return WeeklyDecisionResponse(
            report=report,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            trace=trace_ctx if req.include_trace else None,
        )
    except Exception as e:
        _append_trace_event(
            trace_ctx,
            "runner_error",
            endpoint="/api/weekly-decision-fast",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(
            status_code=500,
            detail=f"快速產出整合決策報告失敗：{str(e)}",
        ) from e


@app.post(
    "/api/review-decision",
    response_model=ReviewDecisionResponse,
    summary="決策審查（Multi-Agent 設備知識校驗）",
    tags=["決策報告"],
)
async def review_decision(req: ReviewDecisionRequest):
    """
    接收 Dir Agent 產出的完整整合決策報告，抽出「## 四、決策指令與資源調度」，
    以 Multi-Agent Swarm（同類型分組批次）依機台設備類型審查每筆維修 Action：
      - SCOP / PRRM / COAT / Unknown 各設備專家 Agent 平行批次審查
      - Aggregator Agent 合併為加上「設備類型 / 審查判定」兩欄的 Final Action Plan
    最後回填取代原 ## 四 區塊，回傳審查後的完整報告。
    """
    if not req.report.strip():
        raise HTTPException(status_code=400, detail="report 不可為空")

    trace_ctx = {
        "request": {
            "endpoint": "/api/review-decision",
            "week_range": req.week_range,
            "report_length": len(req.report or ""),
            "include_trace": req.include_trace,
        },
        "events": [],
    }
    started = time.perf_counter()
    _append_trace_event(trace_ctx, "runner_start", endpoint="/api/review-decision")

    try:
        reviewed_report, stages, meta = await review_decision_report(req.report)

        # 將 swarm 各階段事件寫入主 trace
        for stage in stages:
            stage_name = str(stage.get("stage") or "review_stage")
            _append_trace_event(trace_ctx, f"reviewer_{stage_name}", **stage)

        _append_trace_event(
            trace_ctx,
            "review_done",
            endpoint="/api/review-decision",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            section_four_found=meta.get("section_four_found", False),
            machine_count=meta.get("machine_count", 0),
            equipment_groups=meta.get("equipment_groups", {}),
            reviewed_report_length=len(reviewed_report),
        )
        return ReviewDecisionResponse(
            reviewed_report=reviewed_report,
            section_four_found=bool(meta.get("section_four_found", False)),
            generated_at=datetime.now().isoformat(timespec="seconds"),
            trace=trace_ctx if req.include_trace else None,
        )
    except Exception as e:
        _append_trace_event(
            trace_ctx,
            "runner_error",
            endpoint="/api/review-decision",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            error=str(e),
            traceback=traceback.format_exc(),
        )
        raise HTTPException(
            status_code=500,
            detail=f"決策審查失敗：{str(e)}",
        ) from e


# ═══════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("EXEC_API_HOST", "0.0.0.0")
    port = int(os.getenv("EXEC_API_PORT", "8110"))
    uvicorn.run(app, host=host, port=port)